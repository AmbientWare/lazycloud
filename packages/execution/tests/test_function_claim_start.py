from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier
from uuid import uuid4

from api.server.services import ApiServices
from control.service import ControlPlaneService, StubKind
from database.repositories.container_rollouts import ContainerRolloutRepository
from database.repositories.execution import TaskAttemptRepository, TaskRepository
from database.repositories.orchestration import ContainerRepository
from execution.functions.service import FunctionControlService
from shared.containers import ContainerRecord, ContainerStatus
from shared.function_payloads import FunctionJsonInvocation
from shared.http.functions import FunctionClaimRequest, FunctionRetireRequest
from shared.tasks import Task, TaskStatus
from shared.timestamps import utc_now
from tests.releases import assign_runtime


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


def test_idle_retirement_fences_claims_without_releasing_physical_capacity(
    isolated_services: ApiServices,
) -> None:
    stub = ControlPlaneService(isolated_services.context).create_stub(
        "idle-retirement",
        kind=StubKind.Function,
        handler="main:hello",
        config={"runtime": {"keep_warm": 10}},
    )
    container = ContainerRecord(
        id=str(uuid4()),
        name="retiring",
        image="python",
        command=[],
        workspace_id=stub.workspace_id,
        stub_id=stub.id,
        status=ContainerStatus.Running,
        started_at=utc_now() - timedelta(seconds=30),
    )
    with isolated_services.context.database.session() as session:
        ContainerRepository(session).records.upsert(
            container,
            workspace_id=stub.workspace_id,
            name=container.name,
            status=container.status.value,
        )
    assign_runtime(isolated_services.containers, isolated_services.scheduler_workers, container.id)
    machine_id = str(uuid4())
    with isolated_services.context.database.session() as session:
        assigned = ContainerRepository(session).get_across_workspaces(container.id)
        assert assigned is not None
        assigned.runtime_machine_id = machine_id
        ContainerRepository(session).records.upsert(
            assigned,
            workspace_id=stub.workspace_id,
            name=assigned.name,
            status=assigned.status.value,
        )
    service = FunctionControlService(isolated_services)
    request = FunctionClaimRequest(
        stub_id=stub.id,
        container_id=container.id,
    )
    task = isolated_services.tasks.create(
        "work",
        workspace_id=stub.workspace_id,
        stub_id=stub.id,
        invocation=FunctionJsonInvocation(),
    )
    with isolated_services.context.database.session() as session:
        TaskRepository(session).mark_claimable(task.id, at=utc_now())
    retirement = FunctionRetireRequest(stub_id=stub.id, container_id=container.id)
    assert not service.function_retire(retirement, workspace_id=stub.workspace_id).retired
    claimed = service.function_claim(request)
    assert claimed.task is not None and claimed.task.task_id == task.id
    assert not service.function_retire(retirement, workspace_id=stub.workspace_id).retired

    completed = isolated_services.tasks.transition(task, TaskStatus.Complete)
    assert not service.function_retire(retirement, workspace_id=stub.workspace_id).retired
    with isolated_services.context.database.session() as session:
        attempt = TaskAttemptRepository(session).latest_for_task(completed.id)
        assert attempt is not None
        attempt.finished_at = utc_now() - timedelta(seconds=11)
        TaskAttemptRepository(session).upsert(attempt)
    assert service.function_retire(retirement, workspace_id=stub.workspace_id).retired
    with isolated_services.context.database.session() as session:
        containers = ContainerRepository(session)
        persisted = containers.get_across_workspaces(container.id)
        assert persisted is not None and persisted.status is ContainerStatus.Running
        assert containers.count_live_for_machine(machine_id) == 1
        assert containers.count_live_for_stub(stub.id) == 0
        assert ContainerRolloutRepository(session).serving_floor(stub.id) == 0
        waiting = TaskRepository(session).upsert(
            Task(
                id=str(uuid4()),
                name="next",
                workspace_id=stub.workspace_id,
                stub_id=stub.id,
                claimable_at=utc_now(),
                invocation=FunctionJsonInvocation(),
            )
        )
    assert service.function_retire(retirement, workspace_id=stub.workspace_id).retired
    assert service.function_claim(request).task is None
    with isolated_services.context.database.session() as session:
        queued = TaskRepository(session).get_across_workspaces(waiting.id)
        assert queued is not None and queued.container_id is None
