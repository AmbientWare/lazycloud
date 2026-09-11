from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from uuid import uuid4

from api.server.services import ApiServices
from control.service import ControlPlaneService, StubKind
from database.repositories.execution import TaskAttemptRepository, TaskRepository
from database.repositories.orchestration import ContainerRepository
from shared.containers import ContainerRecord, ContainerStatus
from shared.function_payloads import FunctionJsonInvocation
from shared.tasks import Task, TaskStatus
from shared.timestamps import utc_now


def test_claim_commits_one_running_attempt_before_returning_work(
    isolated_services: ApiServices,
) -> None:
    stub = ControlPlaneService(isolated_services.context).create_stub(
        "claim-start", kind=StubKind.Function, handler="main:hello"
    )
    containers = [
        ContainerRecord(
            id=str(uuid4()),
            name=f"claimant-{index}",
            image="python",
            command=[],
            workspace_id=stub.workspace_id,
            stub_id=stub.id,
            status=ContainerStatus.Running,
        )
        for index in range(2)
    ]
    task = isolated_services.tasks.create(
        "claim-start",
        workspace_id=stub.workspace_id,
        stub_id=stub.id,
        invocation=FunctionJsonInvocation(),
    )
    with isolated_services.context.database.session() as session:
        for container in containers:
            ContainerRepository(session).records.upsert(
                container,
                workspace_id=stub.workspace_id,
                name=container.name,
                status=container.status.value,
            )
        TaskRepository(session).mark_claimable(task.id, at=utc_now())
    barrier = Barrier(2)

    def claim(container: ContainerRecord) -> Task | None:
        barrier.wait(timeout=5)
        return isolated_services.tasks.claim_and_start(stub.id, container_id=container.id)

    with ThreadPoolExecutor(max_workers=2) as executor:
        claims = list(executor.map(claim, containers))
    [claimed] = [item for item in claims if item is not None]
    assert claimed.id == task.id
    assert claimed.status is TaskStatus.Running
    assert claimed.attempt_number == 1
    assert claimed.started_at is not None
    with isolated_services.context.database.session() as session:
        attempt = TaskAttemptRepository(session).latest_for_task(task.id)
        assert attempt is not None
        assert attempt.status is TaskStatus.Running
        assert attempt.container_id == claimed.container_id
        assert attempt.attempt_number == claimed.attempt_number
