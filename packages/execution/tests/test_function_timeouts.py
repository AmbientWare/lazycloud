from datetime import timedelta
from uuid import uuid4

from api.server.services import ApiServices
from compute.policy import WorkspaceComputePolicyService
from control.service import ControlPlaneService, StubKind
from database.repositories.execution import TaskAttemptRepository
from database.repositories.orchestration import ContainerRepository
from execution.functions.service import FunctionControlService
from shared.containers import ContainerRecord, ContainerStatus
from shared.tasks import RetryPolicy, TaskStatus


def test_timeout_fences_retried_attempts_and_recovers_the_container_stop(
    isolated_services: ApiServices,
) -> None:
    services = isolated_services
    stub = ControlPlaneService(
        services.context, placement_resolver=WorkspaceComputePolicyService(services.context)
    ).create_stub(
        "timed-function", kind=StubKind.Function, config={"runtime": {"timeout_seconds": 2}}
    )
    with services.context.database.session() as session:
        container = ContainerRepository(session).create(
            ContainerRecord.model_validate(
                {
                    "id": str(uuid4()),
                    "name": "timed-function",
                    "workspace_id": stub.workspace_id,
                    "stub_id": stub.id,
                    "image": "timeout-test",
                    "command": ["python", "-m", "runner.function"],
                    "status": "running",
                }
            )
        )
    task = services.tasks.create(
        "timed-function",
        workspace_id=stub.workspace_id,
        stub_id=stub.id,
        retry_policy=RetryPolicy(max_attempts=2),
    )
    task = services.tasks.start(task.id, container_id=container.id)
    assert task.started_at is not None
    with services.context.database.session() as session:
        expired = TaskAttemptRepository(session).expired_function_attempts(
            now=task.started_at + timedelta(seconds=3),
            limit=10,
        )
    assert [attempt.task_id for attempt in expired] == [task.id]
    services.tasks.finish_with_retry(task.id, TaskStatus.Failed, container_id=container.id)
    retried = services.tasks.start(task.id, container_id=container.id)
    stale = services.tasks.finish_with_retry(
        task.id,
        TaskStatus.Timeout,
        container_id=container.id,
        attempt_number=expired[0].attempt_number,
    )
    assert not stale.state_changed and stale.task.status is TaskStatus.Running
    assert retried.attempt_number == 2

    services.tasks.finish_with_retry(
        task.id,
        TaskStatus.Timeout,
        container_id=container.id,
        attempt_number=retried.attempt_number,
        error="function execution timed out",
        exit_code=124,
    )
    # The terminal commit survived, but the process stopped before sending the stop.
    functions = FunctionControlService(services)
    functions.expire_timed_out_tasks(now=task.started_at + timedelta(seconds=3))
    functions.expire_timed_out_tasks(now=task.started_at + timedelta(seconds=4))
    assert services.tasks.get(task.id).status is TaskStatus.Timeout
    assert services.containers.get(container.id).status is ContainerStatus.Stopped
    assert [attempt.status for attempt in services.tasks.attempts(task.id)] == [
        TaskStatus.Failed,
        TaskStatus.Timeout,
    ]
