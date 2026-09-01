from __future__ import annotations

from datetime import datetime, timedelta
from uuid import uuid4

from api.server.services import ApiServices
from control.service import ControlPlaneService, StubKind
from database.records.endpoint_dispatch import (
    EndpointDispatchObservationRecord,
    EndpointDispatchStateRecord,
)
from database.repositories.endpoint_dispatch import EndpointDispatchRepository
from database.repositories.orchestration import ContainerRepository
from database.tables.endpoint_dispatch import EndpointDispatchTable
from database.tables.execution import TaskTable
from execution.tasks import TaskService
from shared.containers import ContainerRecord, ContainerStatus
from shared.timestamps import utc_now
from sqlalchemy import delete, func, select


def test_endpoint_dispatch_queries_bound_active_state_and_task_owns_cleanup(
    isolated_services: ApiServices,
) -> None:
    stub = ControlPlaneService(isolated_services.context).create_stub(
        "dispatch-state",
        kind=StubKind.Endpoint,
        handler="module:handler",
    )
    now = utc_now()
    task_service: TaskService = isolated_services.tasks
    active_task = task_service.create(
        "active-dispatch",
        workspace_id=stub.workspace_id,
        stub_id=stub.id,
    )
    stale_task = task_service.create(
        "stale-dispatch",
        workspace_id=stub.workspace_id,
        stub_id=stub.id,
    )
    finished_task = task_service.create(
        "finished-dispatch",
        workspace_id=stub.workspace_id,
        stub_id=stub.id,
    )
    container_id = str(uuid4())

    with isolated_services.context.database.session() as session:
        ContainerRepository(session).upsert(
            ContainerRecord(
                id=container_id,
                name="dispatch-container",
                image="endpoint-image",
                command=["python", "-m", "runner.serve"],
                workspace_id=stub.workspace_id,
                stub_id=stub.id,
                status=ContainerStatus.Running,
            )
        )
        repository = EndpointDispatchRepository(session)
        repository.create(
            _state(
                task_id=active_task.id,
                workspace_id=stub.workspace_id,
                stub_id=stub.id,
                status="inflight",
                at=now,
                expires_at=now + timedelta(minutes=1),
                container_id=container_id,
            )
        )
        repository.create(
            _state(
                task_id=stale_task.id,
                workspace_id=stub.workspace_id,
                stub_id=stub.id,
                status="waiting-capacity",
                at=now - timedelta(minutes=2),
                expires_at=now - timedelta(minutes=1),
            )
        )
        repository.create(
            _state(
                task_id=finished_task.id,
                workspace_id=stub.workspace_id,
                stub_id=stub.id,
                status="complete",
                at=now - timedelta(seconds=5),
                expires_at=now - timedelta(seconds=4),
                container_id=container_id,
                finished_at=now - timedelta(seconds=3),
            )
        )

        assert repository.active_count(stub.id, at=now) == 1
        assert repository.active_counts_by_stub([stub.id], at=now) == {stub.id: 1}
        assert repository.inflight_counts(stub.id, at=now) == {container_id: 1}
        observations = repository.observations_by_stub(
            [stub.id],
            at=now,
            finished_since=now - timedelta(seconds=10),
        )
        assert observations[stub.id] == [
            EndpointDispatchObservationRecord(
                stub_id=stub.id,
                container_id=container_id,
                active=True,
                finished_at=now - timedelta(seconds=3),
            )
        ]
        session.execute(
            delete(TaskTable).where(
                TaskTable.id.in_([active_task.id, stale_task.id, finished_task.id])
            )
        )

    with isolated_services.context.database.session() as session:
        remaining = session.scalar(select(func.count()).select_from(EndpointDispatchTable))
    assert remaining == 0


def _state(
    *,
    task_id: str,
    workspace_id: str,
    stub_id: str,
    status: str,
    at: datetime,
    expires_at: datetime,
    container_id: str | None = None,
    finished_at: datetime | None = None,
) -> EndpointDispatchStateRecord:
    return EndpointDispatchStateRecord(
        task_id=task_id,
        workspace_id=workspace_id,
        stub_id=stub_id,
        container_id=container_id,
        method="POST",
        path="/",
        status=status,
        wait_timeout_seconds=60,
        max_pending_requests=10,
        max_inflight_per_container=1,
        attempts=1,
        enqueued_at=at,
        started_at=at if status == "inflight" else None,
        heartbeat_at=at,
        expires_at=expires_at,
        finished_at=finished_at,
        error=None,
    )
