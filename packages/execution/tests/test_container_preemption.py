from __future__ import annotations

from dataclasses import dataclass

from api.server.services import ApiServices
from control.service import ControlPlaneService
from database.repositories.orchestration import ContainerRepository
from execution.containers.preemption import PreemptedContainerService
from execution.taskqueues.service import TaskQueuePreemptedResult
from shared.containers import ContainerRecord, ContainerStatus
from shared.deployments import StubKind
from shared.tasks import RetryPolicy, Task, TaskStatus


@dataclass(frozen=True, slots=True)
class UnusedTaskQueueControl:
    def task_queue_preempted(
        self,
        *,
        stub_id: str,
        task_id: str,
        container_id: str,
        exit_code: int | None = None,
        error: str = "task queue container was preempted",
    ) -> TaskQueuePreemptedResult:
        del stub_id, task_id, container_id, exit_code, error
        raise AssertionError("non-task-queue preemption must not enter task queue control")


def _running_task(
    services: ApiServices,
    *,
    kind: StubKind,
    name: str,
) -> tuple[Task, ContainerRecord]:
    control = ControlPlaneService(services.context)
    stub = control.create_stub(name, kind=kind)
    task = services.tasks.create(
        name,
        workspace_id=stub.workspace_id,
        stub_id=stub.id,
        retry_policy=RetryPolicy(max_attempts=2),
    )
    with services.context.database.session() as session:
        container = ContainerRepository(session).records.create(
            {
                "name": name,
                "image": "image-preemption-test",
                "command": ["python", "-m", "runner"],
                "workspace_id": stub.workspace_id,
                "stub_id": stub.id,
                "task_id": task.id,
                "status": ContainerStatus.Running.value,
            },
            workspace_id=stub.workspace_id,
            name=name,
            status=ContainerStatus.Running.value,
        )
    task.container_id = container.id
    task.status = TaskStatus.Running
    task.attempt_number = 1
    task = services.tasks.save(task)
    return task, container


def test_function_preemption_uses_explicit_retry_policy(
    isolated_services: ApiServices,
) -> None:
    task, container = _running_task(
        isolated_services,
        kind=StubKind.Function,
        name="preempted-function",
    )
    service = PreemptedContainerService(
        services=isolated_services,
        stubs=isolated_services.control_plane_service,
        task_queues=UnusedTaskQueueControl(),
    )

    result = service.preempted(container, exit_code=562)
    duplicate = service.preempted(container, exit_code=562)

    assert result.task_id == task.id
    assert result.status is TaskStatus.Retry
    assert result.changed
    assert result.retry_scheduled
    assert duplicate.status is TaskStatus.Retry
    assert not duplicate.changed
    assert not duplicate.retry_scheduled


def test_endpoint_preemption_fails_without_blind_replay(
    isolated_services: ApiServices,
) -> None:
    task, container = _running_task(
        isolated_services,
        kind=StubKind.Endpoint,
        name="preempted-endpoint",
    )
    service = PreemptedContainerService(
        services=isolated_services,
        stubs=isolated_services.control_plane_service,
        task_queues=UnusedTaskQueueControl(),
    )

    result = service.preempted(container, exit_code=562)

    assert result.task_id == task.id
    assert result.status is TaskStatus.Failed
    assert result.changed
    assert not result.retry_scheduled
    assert isolated_services.tasks.get(task.id).status is TaskStatus.Failed
