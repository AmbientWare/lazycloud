from dataclasses import dataclass
from datetime import datetime

from database.tables.apps import CronJobTable, DeploymentTable, StubTable
from database.tables.orchestration import ContainerTable
from pydantic import JsonValue
from shared.container_requests import CONTAINER_MEMORY_RESERVATION_PERCENT, capacity_memory_mib
from shared.placement import Placement
from shared.timestamps import to_utc
from shared.workload_config import (
    StubRuntimeConfig,
    requested_cpu_millicores,
    requested_memory_mib,
)
from sqlalchemy import and_, func, or_, select, text
from sqlalchemy.orm import Session


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


@dataclass(slots=True)
class FleetDemandRepository:
    session: Session

    def scheduled(self, *, now: datetime, until: datetime) -> list[FleetDemandRow]:
        """The next known invocation of each enabled platform schedule in the horizon."""
        cron, deployment, stub = CronJobTable, DeploymentTable, StubTable
        rows = self.session.execute(
            select(
                cron.next_run_at,
                stub.runtime_cpu,
                stub.runtime_cpu_millicores,
                stub.runtime_memory,
                stub.runtime_memory_mib,
                stub.runtime_gpu,
                stub.runtime_gpu_count,
                stub.runtime_preemptible,
            )
            .join(deployment, deployment.id == cron.deployment_id)
            .join(stub, stub.id == deployment.stub_id)
            .where(
                cron.enabled.is_(True),
                cron.next_run_at > now,
                cron.next_run_at <= until,
                deployment.active.is_(True),
                deployment.deleted_at.is_(None),
                deployment.placement == Placement.platform().key,
                deployment.machine == "",
            )
        ).tuples()
        result: list[FleetDemandRow] = []
        for at, cpu, cpu_millis, memory, memory_mib, cards, gpu, preemptible in rows:
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
            }
            runtime = StubRuntimeConfig.model_validate(
                {key: value for key, value in values.items() if value is not None}
            )
            result.append(
                FleetDemandRow(
                    observed_at=to_utc(at),
                    preemptible=runtime.preemptible,
                    gpu_types=tuple(runtime.gpu),
                    cpu_millicores=requested_cpu_millicores(runtime.cpu, runtime.cpu_millicores),
                    memory_mib=capacity_memory_mib(
                        requested_memory_mib(runtime.memory, runtime.memory_mib)
                    ),
                    gpu_count=runtime.gpu_count,
                    count=1,
                    pending_count=0,
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
        )
        rows = self.session.execute(
            select(*dimensions, func.count(), func.count().filter(pending))
            .where(
                container.scheduling_placement == Placement.platform().key,
                container.scheduling_requested_at.is_not(None),
                container.scheduling_requested_at <= now,
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
            )
            for at, preemptible, cards, cpu, memory, gpu, count, waiting in rows
        ]
