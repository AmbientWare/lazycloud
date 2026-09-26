from dataclasses import dataclass
from datetime import datetime, timedelta

from database.tables.apps import CronJobTable, DeploymentTable, StubTable
from database.tables.execution import TaskTable
from database.tables.orchestration import ContainerTable
from pydantic import JsonValue
from shared.container_requests import CONTAINER_MEMORY_RESERVATION_PERCENT, capacity_memory_mib
from shared.cron import next_cron_run
from shared.placement import Placement
from shared.timestamps import to_utc
from shared.workload_config import (
    StubRuntimeConfig,
    requested_cpu_millicores,
    requested_memory_mib,
)
from sqlalchemy import and_, func, or_, select, text, type_coerce
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Session
from sqlalchemy.sql.selectable import Subquery


@dataclass(frozen=True, slots=True)
class FleetDemandRow:
    observed_at: datetime
    preemptible: bool
    gpu_types: tuple[str, ...]
    cpu_millicores: int
    memory_mib: int
    gpu_count: int
    count: int
    pending_count: int
    region: str = ""
    availability_zone: str = ""
    architecture: str = ""
    runtime: str = ""
    duration_seconds: float | None = None
    workload_id: str = ""
    concurrency: int = 1
    keep_warm_seconds: int = 0
    existing_containers: int = 0
    max_containers: int = 1


@dataclass(frozen=True, slots=True)
class FleetBacklogRow:
    demand: FleetDemandRow
    queued_tasks: int
    tasks_per_container: int


def _live_containers() -> Subquery:
    return (
        select(ContainerTable.stub_id, func.count().label("containers"))
        .where(ContainerTable.status.in_(("pending", "running")))
        .group_by(ContainerTable.stub_id)
        .subquery()
    )


@dataclass(slots=True)
class FleetDemandRepository:
    session: Session

    def scheduled(self, *, now: datetime, until: datetime) -> list[FleetDemandRow]:
        """Scheduled platform invocations within the bounded provision horizon."""
        cron, deployment, stub = CronJobTable, DeploymentTable, StubTable
        live = _live_containers()
        duration = (
            select(
                func.percentile_cont(0.95).within_group(
                    func.extract("epoch", TaskTable.finished_at - TaskTable.started_at)
                )
            )
            .where(
                TaskTable.stub_id == stub.id,
                TaskTable.created_at > now - timedelta(minutes=10),
                TaskTable.finished_at <= now,
                TaskTable.finished_at > TaskTable.started_at,
            )
            .correlate(stub)
            .scalar_subquery()
        )
        rows = self.session.execute(
            select(
                cron.next_run_at,
                cron.cron,
                stub.runtime_cpu,
                stub.runtime_cpu_millicores,
                stub.runtime_memory,
                stub.runtime_memory_mib,
                stub.runtime_gpu,
                stub.runtime_gpu_count,
                stub.runtime_preemptible,
                stub.runtime_region,
                stub.runtime_availability_zone,
                stub.runtime_runtime,
                stub.id,
                stub.runtime_concurrency,
                stub.runtime_keep_warm,
                stub.autoscaler_max_containers,
                func.coalesce(live.c.containers, 0),
                duration,
            )
            .join(deployment, deployment.id == cron.deployment_id)
            .join(stub, stub.id == deployment.stub_id)
            .outerjoin(live, live.c.stub_id == stub.id)
            .where(
                cron.enabled.is_(True),
                cron.next_run_at > now,
                cron.next_run_at <= until,
                deployment.active.is_(True),
                deployment.deleted_at.is_(None),
                deployment.placement == Placement.platform().key,
                deployment.machine == "",
                stub.type == "function",
                func.coalesce(
                    func.jsonb_array_length(type_coerce(stub.configuration, JSONB)["disks"]), 0
                )
                == 0,
            )
        ).tuples()
        result: list[FleetDemandRow] = []
        for (
            at,
            expression,
            cpu,
            cpu_millis,
            memory,
            memory_mib,
            cards,
            gpu,
            preemptible,
            region,
            zone,
            runtime_name,
            workload_id,
            concurrency,
            keep_warm,
            max_containers,
            existing_containers,
            observed_duration,
        ) in rows:
            if at is None:
                raise ValueError("a scheduled forecast row has no next invocation time")
            values: dict[str, JsonValue | list[str]] = {
                "cpu": cpu,
                "cpu_millicores": cpu_millis,
                "memory": memory,
                "memory_mib": memory_mib,
                "gpu": cards,
                "gpu_count": gpu,
                "preemptible": preemptible,
                "runtime": runtime_name,
                "concurrency": concurrency,
                "keep_warm": keep_warm,
            }
            runtime = StubRuntimeConfig.model_validate(
                {key: value for key, value in values.items() if value is not None}
            )
            occurrence = to_utc(at)
            while occurrence <= until:
                result.append(
                    FleetDemandRow(
                        observed_at=occurrence,
                        preemptible=runtime.preemptible,
                        gpu_types=tuple(runtime.gpu),
                        cpu_millicores=requested_cpu_millicores(
                            runtime.cpu, runtime.cpu_millicores
                        ),
                        memory_mib=capacity_memory_mib(
                            requested_memory_mib(runtime.memory, runtime.memory_mib)
                        ),
                        gpu_count=runtime.gpu_count,
                        count=1,
                        pending_count=0,
                        region=region or "",
                        availability_zone=zone or "",
                        runtime=runtime.runtime,
                        workload_id=str(workload_id),
                        concurrency=runtime.concurrency,
                        keep_warm_seconds=runtime.keep_warm,
                        max_containers=max_containers if max_containers is not None else 1,
                        existing_containers=existing_containers,
                        duration_seconds=float(observed_duration)
                        if observed_duration is not None
                        else None,
                    )
                )
                occurrence = next_cron_run(expression, occurrence)
        return result

    def backlog(self, *, now: datetime) -> list[FleetBacklogRow]:
        """Runnable function work not yet represented by container requests."""
        stub, task, deployment = StubTable, TaskTable, DeploymentTable
        queued = (
            select(task.stub_id, func.count().label("tasks"))
            .where(
                task.status == "pending",
                task.container_id.is_(None),
                task.claimable_at <= now,
            )
            .group_by(task.stub_id)
            .subquery()
        )
        live = _live_containers()
        rows = self.session.execute(
            select(
                stub.id,
                stub.runtime_cpu,
                stub.runtime_cpu_millicores,
                stub.runtime_memory,
                stub.runtime_memory_mib,
                stub.runtime_gpu,
                stub.runtime_gpu_count,
                stub.runtime_preemptible,
                stub.runtime_region,
                stub.runtime_availability_zone,
                stub.runtime_runtime,
                stub.runtime_concurrency,
                stub.autoscaler_max_containers,
                stub.autoscaler_tasks_per_container,
                queued.c.tasks,
                func.coalesce(live.c.containers, 0),
            )
            .join(queued, queued.c.stub_id == stub.id)
            .join(deployment, deployment.id == stub.deployment_id)
            .outerjoin(live, live.c.stub_id == stub.id)
            .where(
                stub.type == "function",
                deployment.active.is_(True),
                deployment.deleted_at.is_(None),
                deployment.placement == Placement.platform().key,
                deployment.machine == "",
                func.coalesce(
                    func.jsonb_array_length(type_coerce(stub.configuration, JSONB)["disks"]), 0
                )
                == 0,
            )
        ).tuples()
        result: list[FleetBacklogRow] = []
        for (
            identity,
            cpu,
            cpu_millis,
            memory,
            memory_mib,
            cards,
            gpu,
            preemptible,
            region,
            zone,
            runtime_name,
            concurrency,
            maximum,
            tasks_per_container,
            tasks,
            existing,
        ) in rows:
            values: dict[str, JsonValue | list[str]] = {
                "cpu": cpu,
                "cpu_millicores": cpu_millis,
                "memory": memory,
                "memory_mib": memory_mib,
                "gpu": cards,
                "gpu_count": gpu,
                "preemptible": preemptible,
                "concurrency": concurrency,
                "runtime": runtime_name,
            }
            runtime = StubRuntimeConfig.model_validate(
                {key: value for key, value in values.items() if value is not None}
            )
            result.append(
                FleetBacklogRow(
                    demand=FleetDemandRow(
                        observed_at=now,
                        preemptible=runtime.preemptible,
                        gpu_types=tuple(runtime.gpu),
                        cpu_millicores=requested_cpu_millicores(
                            runtime.cpu, runtime.cpu_millicores
                        ),
                        memory_mib=capacity_memory_mib(
                            requested_memory_mib(runtime.memory, runtime.memory_mib)
                        ),
                        gpu_count=runtime.gpu_count,
                        count=0,
                        pending_count=0,
                        region=region or "",
                        availability_zone=zone or "",
                        runtime=runtime.runtime,
                        workload_id=str(identity),
                        concurrency=runtime.concurrency,
                        existing_containers=existing,
                        max_containers=maximum if maximum is not None else 1,
                    ),
                    queued_tasks=tasks,
                    tasks_per_container=tasks_per_container
                    if tasks_per_container is not None
                    else 1,
                )
            )
        return result

    def recent(self, *, since: datetime, now: datetime) -> list[FleetDemandRow]:
        container = ContainerTable
        pending = and_(container.status == "pending", container.runtime_worker_id == "")
        bucket = func.date_bin(
            text("INTERVAL '10 seconds'"),
            container.scheduling_requested_at,
            text("TIMESTAMPTZ '2000-01-01 00:00:00+00'"),
        )
        dimensions = (
            bucket,
            container.scheduling_preemptible,
            container.scheduling_gpu,
            container.scheduling_cpu_millicores,
            container.scheduling_memory_mib,
            container.scheduling_gpu_count,
            container.scheduling_region,
            container.scheduling_availability_zone,
            container.scheduling_architecture,
            container.scheduling_provider_runtime,
        )
        rows = self.session.execute(
            select(
                *dimensions,
                func.count(),
                func.count().filter(pending),
                func.avg(
                    func.extract("epoch", container.finished_at - container.started_at)
                ).filter(container.finished_at.is_not(None), container.started_at.is_not(None)),
            )
            .where(
                container.scheduling_placement == Placement.platform().key,
                container.scheduling_requested_at.is_not(None),
                container.scheduling_requested_at <= now,
                container.scheduling_required_worker_id == "",
                container.scheduling_disk_count == 0,
                container.scheduling_disk_bytes == 0,
                or_(container.scheduling_requested_at > since, pending),
            )
            .group_by(*dimensions)
        ).tuples()
        return [
            FleetDemandRow(
                observed_at=to_utc(at),
                preemptible=preemptible,
                gpu_types=tuple(cards),
                cpu_millicores=cpu,
                memory_mib=(memory * CONTAINER_MEMORY_RESERVATION_PERCENT + 99) // 100,
                gpu_count=gpu,
                count=count,
                pending_count=waiting,
                region=region or "",
                availability_zone=zone,
                architecture=architecture,
                runtime=runtime,
                duration_seconds=float(duration) if duration is not None and duration > 0 else None,
            )
            for (
                at,
                preemptible,
                cards,
                cpu,
                memory,
                gpu,
                region,
                zone,
                architecture,
                runtime,
                count,
                waiting,
                duration,
            ) in rows
        ]
