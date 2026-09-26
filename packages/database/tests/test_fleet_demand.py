from datetime import timedelta
from uuid import uuid4

from database.mappers.containers import write_container
from database.records.apps import StubRecord
from database.repositories.apps import CronJobRepository, DeploymentRepository, StubRepository
from database.repositories.fleet_demand import FleetDemandRepository
from database.tables.orchestration import ContainerTable
from identity.platform import PlatformNamespaceService
from shared.container_requests import capacity_memory_mib
from shared.containers import ContainerRecord, ContainerStatus
from shared.cron import CronJobRecord
from shared.deployment_records import Deployment, DeploymentSpec
from shared.deployments import DeploymentKind
from shared.placement import Placement
from shared.timestamps import utc_now
from shared.workload_config import StubConfig, StubRuntimeConfig
from sqlalchemy import text

from database import DatabaseClient


def test_schedule_forecast_uses_persisted_resources_and_excludes_private_capacity(
    database: DatabaseClient,
) -> None:
    workspace_id = PlatformNamespaceService(database).initialize().id
    now = utc_now()
    due = now + timedelta(seconds=30)
    with database.session() as session:
        for placement in (Placement.platform(), Placement.machine(str(uuid4()))):
            identity = str(uuid4())
            stub = StubRepository(session).upsert(
                StubRecord(
                    id=identity,
                    workspace_id=workspace_id,
                    name=identity,
                    config=StubConfig(runtime=StubRuntimeConfig(cpu=2, memory="4Gi")),
                )
            )
            deployment = DeploymentRepository(session).upsert(
                Deployment(
                    id=identity,
                    name=identity,
                    kind=DeploymentKind.Function,
                    stub_id=stub.id,
                    spec=DeploymentSpec(name=identity),
                    subdomain=identity,
                    placement=placement,
                ),
                workspace_id=workspace_id,
            )
            CronJobRepository(session).upsert(
                CronJobRecord(
                    workspace_id=workspace_id,
                    name=identity,
                    cron="* * * * *",
                    deployment_id=deployment.id,
                    next_run_at=due,
                ),
                workspace_id=workspace_id,
            )
        repository = FleetDemandRepository(session)
        assert repository.scheduled(now=now, until=due - timedelta(seconds=1)) == []
        [row] = repository.scheduled(now=now, until=due)
        assert row.cpu_millicores == 2000
        assert row.memory_mib == capacity_memory_mib(4096)
        assert row.observed_at == due
        assert row.count == 1 and row.pending_count == 0


def test_forecast_reads_recent_arrivals_and_unassigned_backlog_without_retained_history(
    database: DatabaseClient,
) -> None:
    workspace_id = PlatformNamespaceService(database).initialize().id
    now = utc_now().replace(microsecond=0)
    old = now - timedelta(days=2)
    with database.session() as session:
        for _ in range(1000):
            record = ContainerRecord(
                id=str(uuid4()),
                workspace_id=workspace_id,
                name="retained-container",
                image="history",
                command=[],
                status=ContainerStatus.Exited,
                env={"retained": "x" * 16_384},
            )
            row = ContainerTable(id=record.id, scheduling_requested_at=old)
            write_container(row, record)
            row.scheduling_placement = Placement.platform().key
            session.add(row)
        session.flush()
        session.execute(text("ANALYZE containers"))
        assert (
            FleetDemandRepository(session).recent(since=now - timedelta(minutes=10), now=now) == []
        )
        for at, status, worker, placement in (
            (old, ContainerStatus.Pending, "", Placement.platform()),
            (old, ContainerStatus.Pending, "assigned", Placement.platform()),
            (now - timedelta(seconds=5), ContainerStatus.Pending, "", Placement.platform()),
            (now - timedelta(seconds=5), ContainerStatus.Running, "assigned", Placement.platform()),
            (now + timedelta(seconds=5), ContainerStatus.Pending, "", Placement.platform()),
            (
                now - timedelta(seconds=5),
                ContainerStatus.Pending,
                "",
                Placement.machine(str(uuid4())),
            ),
        ):
            record = ContainerRecord(
                id=str(uuid4()),
                workspace_id=workspace_id,
                name="demand-container",
                image="current",
                command=[],
                status=status,
                runtime_worker_id=worker,
            )
            row = ContainerTable(id=record.id, scheduling_requested_at=at)
            write_container(row, record)
            row.scheduling_placement = placement.key
            row.scheduling_cpu_millicores = 2000
            row.scheduling_memory_mib = 4096
            row.scheduling_preemptible = False
            session.add(row)
        session.flush()
        session.execute(text("ANALYZE containers"))
        rows = FleetDemandRepository(session).recent(since=now - timedelta(minutes=10), now=now)
    assert sum(row.count for row in rows) == 3
    assert sum(row.pending_count for row in rows) == 2
    assert all(row.cpu_millicores == 2000 for row in rows)
    assert all(row.memory_mib == capacity_memory_mib(4096) for row in rows)
    assert sum(row.count for row in rows if row.observed_at < now - timedelta(minutes=10)) == 1
