from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier
from uuid import uuid4

import pytest
from api.server.services import ApiServices
from control.service import ControlPlaneService, StubKind
from database.repositories.container_rollouts import ContainerRolloutRepository
from database.repositories.execution import TaskAttemptRepository, TaskRepository
from database.repositories.orchestration import ContainerRepository
from database.tables.execution import TaskAttemptTable
from execution.functions.service import FunctionControlService
from execution.task_claims import TaskClaimReleaseService
from observability.startup_latency import StartupLatencyService
from shared.containers import ContainerRecord, ContainerStatus
from shared.function_payloads import FunctionJsonInvocation
from shared.http.execution_entry import ExecutionEntryEvidence
from shared.http.functions import (
    FunctionClaimRequest,
    FunctionRetireRequest,
)
from shared.http.workspace_changes import WorkspaceChangeType
from shared.tasks import Task, TaskStatus
from shared.timestamps import utc_now
from sqlalchemy import select
from tests.releases import assign_runtime


def test_claim_commits_one_running_attempt_before_returning_work(
    isolated_services: ApiServices,
) -> None:
    stub = ControlPlaneService(
        isolated_services.context,
    ).create_stub("claim-start", kind=StubKind.Function, handler="main:hello")
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
            ContainerRepository(session).upsert(container)
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


def test_retried_claim_returns_the_task_it_already_took(
    isolated_services: ApiServices,
) -> None:
    stub = ControlPlaneService(
        isolated_services.context,
    ).create_stub("claim-retry", kind=StubKind.Function, handler="main:hello")
    container = ContainerRecord(
        id=str(uuid4()),
        name="claim-retry",
        image="python",
        command=[],
        workspace_id=stub.workspace_id,
        stub_id=stub.id,
        status=ContainerStatus.Running,
    )
    tasks = [
        isolated_services.tasks.create(
            f"claim-retry-{index}",
            workspace_id=stub.workspace_id,
            stub_id=stub.id,
            invocation=FunctionJsonInvocation(),
        )
        for index in range(2)
    ]
    with isolated_services.context.database.session() as session:
        ContainerRepository(session).upsert(container)
        for task in tasks:
            TaskRepository(session).mark_claimable(task.id, at=utc_now())
    lost_claim = str(uuid4())

    def claim(claim_id: str) -> Task | None:
        return isolated_services.tasks.claim_and_start(
            stub.id, container_id=container.id, claim_id=claim_id
        )

    first = claim(lost_claim)
    retried = claim(lost_claim)
    fresh = claim(str(uuid4()))
    assert first is not None and retried is not None and fresh is not None
    assert retried.id == first.id
    assert fresh.id != first.id
    with isolated_services.context.database.session() as session:
        assert len(TaskAttemptRepository(session).list_for_task(first.id)) == 1
    entry = ExecutionEntryEvidence(
        elapsed_since_entry_seconds=0,
    )
    with isolated_services.context.database.session() as session:
        TaskClaimReleaseService(session).release(first.id, container_id=container.id)
    resumed_claim = str(uuid4())
    resumed = claim(resumed_claim)
    assert resumed is not None and resumed.id == first.id
    assert resumed.attempt_number == first.attempt_number
    stale = isolated_services.tasks.finish_with_retry(
        first.id,
        TaskStatus.Complete,
        container_id=container.id,
        claim_id=lost_claim,
    )
    assert not stale.state_changed
    assert stale.task.status is TaskStatus.Running
    with isolated_services.context.database.session() as session:
        assert (
            session.scalar(
                select(TaskAttemptTable.execution_entry_upper_bound_at).where(
                    TaskAttemptTable.task_id == first.id
                )
            )
            is None
        )
    finished = isolated_services.tasks.finish_with_retry(
        first.id,
        TaskStatus.Complete,
        container_id=container.id,
        execution_entry=entry,
        claim_id=resumed_claim,
    )
    assert finished.state_changed
    with isolated_services.context.database.session() as session:
        original = session.scalar(
            select(TaskAttemptTable.execution_entry_upper_bound_at).where(
                TaskAttemptTable.task_id == first.id
            )
        )
    assert original is not None
    repeated = isolated_services.tasks.finish_with_retry(
        first.id,
        TaskStatus.Complete,
        container_id=container.id,
        execution_entry=entry,
        claim_id=resumed_claim,
    )
    assert not repeated.state_changed
    with isolated_services.context.database.session() as session:
        assert (
            session.scalar(
                select(TaskAttemptTable.execution_entry_upper_bound_at).where(
                    TaskAttemptTable.task_id == first.id
                )
            )
            == original
        )
    report = StartupLatencyService(isolated_services.context.database).read(
        workspace_id=stub.workspace_id
    )
    assert report.execution.reported_entries == 1
    assert report.execution.missing_entry_evidence == 1


@pytest.mark.anyio
async def test_long_claim_receives_committed_work_and_preserves_retry_identity(
    async_services: ApiServices,
) -> None:
    stub = ControlPlaneService(async_services.context).create_stub(
        "long-claim", kind=StubKind.Function, handler="main:hello"
    )
    container = ContainerRecord(
        id=str(uuid4()),
        name="long-claim",
        image="python",
        command=[],
        workspace_id=stub.workspace_id,
        stub_id=stub.id,
        status=ContainerStatus.Pending,
    )
    with async_services.context.database.session() as session:
        ContainerRepository(session).upsert(container)
    assign_runtime(async_services.containers, async_services.scheduler_workers, container.id)
    with async_services.context.database.session() as session:
        assigned = ContainerRepository(session).get_across_workspaces(container.id)
        assert assigned is not None
        assigned.status = ContainerStatus.Running
        ContainerRepository(session).upsert(assigned)
    service = async_services.function_service
    request = FunctionClaimRequest(
        stub_id=stub.id,
        container_id=container.id,
        claim_id=str(uuid4()),
        wait_seconds=2,
    )
    claimed = asyncio.create_task(
        service.function_claim_wait(request, workspace_id=stub.workspace_id)
    )
    await asyncio.sleep(0.05)
    task = async_services.tasks.create(
        "long-claim",
        workspace_id=stub.workspace_id,
        stub_id=stub.id,
        invocation=FunctionJsonInvocation(),
    )
    with async_services.context.database.session() as session:
        runnable = TaskRepository(session).mark_claimable(task.id, at=utc_now())
    assert runnable is not None
    async_services.tasks.publish_lifecycle_change(runnable, WorkspaceChangeType.Updated)
    response = await claimed
    assert response.task is not None and response.task.task_id == task.id
    retried = await service.function_claim_wait(request, workspace_id=stub.workspace_id)
    assert retried.task is not None and retried.task.task_id == task.id
    recovery = asyncio.create_task(
        service.function_claim_wait(
            request.model_copy(update={"claim_id": str(uuid4()), "wait_seconds": 6}),
            workspace_id=stub.workspace_id,
        )
    )
    await asyncio.sleep(0.05)
    missed = async_services.tasks.create(
        "missed-notification",
        workspace_id=stub.workspace_id,
        stub_id=stub.id,
        invocation=FunctionJsonInvocation(),
    )
    with async_services.context.database.session() as session:
        TaskRepository(session).mark_claimable(missed.id, at=utc_now())
    recovered = await recovery
    assert recovered.task is not None and recovered.task.task_id == missed.id


def test_idle_retirement_fences_claims_without_releasing_physical_capacity(
    isolated_services: ApiServices,
) -> None:
    stub = ControlPlaneService(
        isolated_services.context,
    ).create_stub(
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
        status=ContainerStatus.Pending,
    )
    with isolated_services.context.database.session() as session:
        ContainerRepository(session).upsert(container)
    assign_runtime(isolated_services.containers, isolated_services.scheduler_workers, container.id)
    with isolated_services.context.database.session() as session:
        assigned = ContainerRepository(session).get_across_workspaces(container.id)
        assert assigned is not None
        machine_id = assigned.runtime_machine_id
        assigned.status = ContainerStatus.Running
        assigned.started_at = utc_now() - timedelta(seconds=30)
        ContainerRepository(session).upsert(assigned)
    service = FunctionControlService(isolated_services)
    request = FunctionClaimRequest(
        stub_id=stub.id,
        container_id=container.id,
        claim_id=str(uuid4()),
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
