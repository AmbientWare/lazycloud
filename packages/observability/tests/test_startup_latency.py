from datetime import timedelta
from uuid import uuid4

from api.server.services import ApiServices
from control.service import ControlPlaneService
from database.repositories.orchestration import ContainerRepository
from database.tables.orchestration import ContainerTable
from observability.startup_latency import StartupLatencyService
from shared.containers import ContainerRecord, ContainerStatus
from shared.deployment_records import DeploymentSpec
from shared.timestamps import utc_now
from sqlalchemy import update


def test_startup_report_keeps_failed_and_overdue_containers_in_the_cohort(
    isolated_services: ApiServices,
) -> None:
    deployment = isolated_services.deployments.deploy(
        DeploymentSpec(name="startup-latency", handler="pkg.module:handler")
    )
    stub = next(
        item
        for item in ControlPlaneService(isolated_services.context).list_stubs()
        if item.deployment_id == deployment.id
    )
    now = utc_now()
    created = now - timedelta(seconds=30)
    with isolated_services.context.database.session() as session:
        for duration, status, age in (
            (1, ContainerStatus.Running, 30),
            (5, ContainerStatus.Running, 30),
            (None, ContainerStatus.Pending, 30),
            (None, ContainerStatus.Failed, 30),
            (None, ContainerStatus.Pending, 1),
            (8, ContainerStatus.Running, 7200),
        ):
            container = ContainerRecord(
                id=str(uuid4()),
                name="latency-container",
                image="latency-image",
                command=[],
                workspace_id=stub.workspace_id,
                stub_id=stub.id,
                status=status,
            )
            ContainerRepository(session).upsert(container)
            requested = now - timedelta(seconds=age)
            session.execute(
                update(ContainerTable)
                .where(ContainerTable.id == container.id)
                .values(
                    created_at=requested,
                    scheduling_requested_at=requested,
                    scheduling_assigned_at=requested + timedelta(milliseconds=100)
                    if duration is not None
                    else None,
                    started_at=requested + timedelta(milliseconds=200)
                    if duration is not None
                    else None,
                    workload_ready_at=requested + timedelta(seconds=duration)
                    if duration is not None
                    else None,
                )
            )
    report = StartupLatencyService(isolated_services.context.database).read(
        workspace_id=stub.workspace_id, now=now
    )
    assert report.since < created
    assert report.functions.requested == 5
    assert report.functions.ready == 2
    assert report.functions.ready_after_deadline == 1
    assert report.functions.overdue_without_readiness == 2
    assert report.functions.failed_before_readiness == 1
    assert report.functions.request_to_ready.observed == 2
    assert report.functions.request_to_ready.p50_seconds == 3
    assert report.warm_execution_measurement == "unavailable"
    empty = StartupLatencyService(isolated_services.context.database).read(
        workspace_id=str(uuid4()), now=now
    )
    assert empty.functions.requested == 0
    assert empty.functions.request_to_ready.p50_seconds is None
