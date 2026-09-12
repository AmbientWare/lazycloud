from datetime import timedelta
from unittest.mock import patch
from uuid import uuid4

import pytest
from api.server.services import ApiServices
from control.service import ControlPlaneService, StubKind
from database.repositories.container_rollouts import ContainerRolloutRepository
from database.repositories.execution import TaskAttemptRepository, TaskRepository
from database.repositories.orchestration import ContainerRepository
from database.tables.task_callbacks import TaskCallbackTable
from execution.functions.service import FunctionControlService
from observability.events import EventService
from shared.container_requests import StopContainerReason
from shared.containers import ContainerRecord, ContainerStatus
from shared.function_payloads import FunctionJsonInvocation, FunctionJsonResult
from shared.http.functions import FunctionSetResultBody
from shared.tasks import RetryPolicy, TaskStatus
from shared.timestamps import utc_now
from sqlalchemy import select


def test_late_result_times_out_and_releases_pooled_sibling_without_charging_attempt(
    isolated_services: ApiServices,
) -> None:
    services = isolated_services
    stub = ControlPlaneService(services.context).create_stub(
        "deadline-pool",
        kind=StubKind.Function,
        config={
            "runtime": {"timeout_seconds": 3},
            "callback_url": "https://callbacks.example.com/task",
        },
    )
    containers = [
        ContainerRecord(
            id=str(uuid4()),
            name=f"deadline-{index}",
            image="python",
            command=[],
            workspace_id=stub.workspace_id,
            stub_id=stub.id,
            status=ContainerStatus.Running,
        )
        for index in range(2)
    ]
    with services.context.database.session() as session:
        for container in containers:
            ContainerRepository(session).records.upsert(
                container,
                workspace_id=stub.workspace_id,
                name=container.name,
                status=container.status.value,
            )
    tasks = [
        services.tasks.create(
            f"deadline-{index}",
            workspace_id=stub.workspace_id,
            stub_id=stub.id,
            invocation=FunctionJsonInvocation(),
        )
        for index in range(2)
    ]
    for task in tasks:
        services.tasks.start(task.id, container_id=containers[0].id)
    with services.context.database.session() as session:
        attempts = TaskAttemptRepository(session)
        expired = attempts.latest_for_task(tasks[0].id)
        sibling = attempts.latest_for_task(tasks[1].id)
        assert expired is not None and sibling is not None
        assert expired.started_at is not None
        assert expired.deadline_at == expired.started_at + timedelta(seconds=3)
        expired.deadline_at = utc_now() - timedelta(seconds=1)
        attempts.upsert(expired)

    functions = FunctionControlService(services)
    body = FunctionSetResultBody(
        task_id=tasks[0].id, container_id=containers[0].id, result=FunctionJsonResult(value=42)
    )
    response = functions.function_set_result(body)
    assert not response.stored and response.status is TaskStatus.Timeout
    timed_out = services.tasks.get(tasks[0].id)
    assert timed_out.function_result is None and timed_out.exit_code == 124
    stopped = services.containers.get(containers[0].id)
    assert stopped.status is ContainerStatus.Stopped
    assert stopped.termination_reason is StopContainerReason.Scheduler
    assert not functions.function_set_result(body).stored
    assert services.tasks.transition(tasks[0], TaskStatus.Complete).status is TaskStatus.Timeout
    with services.context.database.session() as session:
        [callback] = session.scalars(select(TaskCallbackTable)).all()
        assert callback.status == "pending"
        assert callback.payload["status"] == "timeout"
    released = services.tasks.get(tasks[1].id)
    assert released.status is TaskStatus.Pending and released.container_id is None
    assert released.attempt_number == 1
    services.tasks.start(released.id, container_id=containers[1].id)
    with services.context.database.session() as session:
        replay = TaskAttemptRepository(session).latest_for_task(released.id)
        assert replay is not None and replay.id == sibling.id
        assert replay.deadline_at is not None and sibling.deadline_at is not None
        assert replay.deadline_at > sibling.deadline_at
    assert services.tasks.get(released.id).attempt_number == 1


def test_deadline_stop_recovers_after_event_failure_and_retry_gets_a_fresh_deadline(
    isolated_services: ApiServices,
) -> None:
    services = isolated_services
    stub = ControlPlaneService(services.context).create_stub(
        "deadline-retry", kind=StubKind.Function, config={"runtime": {"timeout_seconds": 3}}
    )
    containers = [
        ContainerRecord(
            id=str(uuid4()),
            name=f"deadline-retry-{index}",
            image="python",
            command=[],
            workspace_id=stub.workspace_id,
            stub_id=stub.id,
            status=ContainerStatus.Running,
        )
        for index in range(2)
    ]
    with services.context.database.session() as session:
        for container in containers:
            ContainerRepository(session).records.upsert(
                container,
                workspace_id=stub.workspace_id,
                name=container.name,
                status=container.status.value,
            )
    task = services.tasks.create(
        "deadline-retry",
        workspace_id=stub.workspace_id,
        stub_id=stub.id,
        invocation=FunctionJsonInvocation(),
        retry_policy=RetryPolicy(max_attempts=2),
    )
    services.tasks.start(task.id, container_id=containers[0].id)
    with services.context.database.session() as session:
        attempts = TaskAttemptRepository(session)
        first = attempts.latest_for_task(task.id)
        assert first is not None
        first.deadline_at = utc_now() - timedelta(seconds=1)
        attempts.upsert(first)
    functions = FunctionControlService(services)
    with (
        patch.object(
            EventService, "emit", side_effect=RuntimeError("event publication unavailable")
        ),
        pytest.raises(RuntimeError, match="event publication unavailable"),
    ):
        functions.expire_function_attempts()
    settled = services.tasks.get(task.id)
    assert settled.status is TaskStatus.Retry and settled.container_id is None
    assert services.containers.get(containers[0].id).status is ContainerStatus.Running
    with services.context.database.session() as session:
        assert not ContainerRolloutRepository(session).accepting_work(
            containers[0].id, stub_id=stub.id
        )
        assert TaskAttemptRepository(session).expired_function_attempts(now=utc_now(), limit=10)

    functions.expire_function_attempts()
    assert services.containers.get(containers[0].id).status is ContainerStatus.Stopped
    functions.schedule_due_retries()
    with services.context.database.session() as session:
        TaskRepository(session).mark_claimable(task.id, at=utc_now())
    retried = services.tasks.claim_and_start(stub.id, container_id=containers[1].id)
    assert retried is not None and retried.attempt_number == 2
    with services.context.database.session() as session:
        second = TaskAttemptRepository(session).latest_for_task(task.id)
        assert second is not None and second.id != first.id
        assert second.started_at is not None
        assert second.deadline_at == second.started_at + timedelta(seconds=3)
    assert not services.tasks.expire_function_attempt(first, now=utc_now()).state_changed
    assert functions.function_set_result(
        FunctionSetResultBody(
            task_id=task.id, container_id=containers[1].id, result=FunctionJsonResult(value=42)
        )
    ).stored

    unlimited = ControlPlaneService(services.context).create_stub(
        "deadline-unlimited", kind=StubKind.Function, config={"runtime": {"timeout_seconds": 0}}
    )
    unbounded = services.tasks.create(
        "deadline-unlimited",
        workspace_id=stub.workspace_id,
        stub_id=unlimited.id,
        invocation=FunctionJsonInvocation(),
    )
    services.tasks.start(unbounded.id)
    with services.context.database.session() as session:
        attempt = TaskAttemptRepository(session).latest_for_task(unbounded.id)
        assert attempt is not None and attempt.deadline_at is None
