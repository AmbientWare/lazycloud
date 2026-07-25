from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime

from api.server.services import ApiServices
from control.service import ControlPlaneService, StubKind
from database.repositories.orchestration import ContainerRepository
from execution.functions.service import FunctionControlService
from shared.containers import ContainerStatus
from shared.function_payloads import FunctionJsonInvocation
from shared.http.functions import FunctionInvokeBody
from shared.scheduling import (
    SchedulerContainerSubmitResult,
    SchedulerContainerSubmitStatus,
    SchedulerWorkerRequest,
)
from shared.tasks import TaskStatus


class _RecordingScheduler:
    def __init__(self) -> None:
        self.requests: list[SchedulerWorkerRequest] = []

    def submit(
        self,
        request: SchedulerWorkerRequest,
        *,
        ready_at: datetime | None = None,
    ) -> SchedulerContainerSubmitResult:
        del ready_at
        self.requests.append(request)
        return SchedulerContainerSubmitResult(
            status=SchedulerContainerSubmitStatus.Queued,
            container_id=request.container_id,
        )


def test_function_retry_waits_for_previous_container_to_become_terminal(
    isolated_services: ApiServices,
) -> None:
    scheduler = _RecordingScheduler()
    isolated_services = replace(
        isolated_services,
        containers=replace(isolated_services.containers, scheduler=scheduler),
    )
    stub = ControlPlaneService(isolated_services.context).create_stub(
        "retry-container-lifecycle",
        kind=StubKind.Function,
        handler="module:handler",
        config={"runtime": {"image_id": "image", "retries": 1}},
    )
    functions = FunctionControlService(isolated_services)
    invoked = functions.function_invoke(
        FunctionInvokeBody(
            stub_id=stub.id,
            invocation=FunctionJsonInvocation(args=[1]),
        )
    )
    first_container_id = scheduler.requests[0].container_id
    isolated_services.tasks.start(invoked.task_id, container_id=first_container_id)

    retry = functions.finish_function_task(
        invoked.task_id,
        TaskStatus.Failed,
        container_id=first_container_id,
        error="retry",
        exit_code=1,
    )

    assert retry.status is TaskStatus.Retry
    assert retry.container_id is None
    assert len(scheduler.requests) == 1
    assert functions.schedule_due_retries(now=datetime.now(UTC)) == []
    assert len(scheduler.requests) == 1

    first_container = isolated_services.containers.get(first_container_id)
    first_container.status = ContainerStatus.Failed
    first_container.exit_code = 1
    first_container.finished_at = datetime.now(UTC)
    with isolated_services.context.database.session() as session:
        ContainerRepository(session).records.upsert(
            first_container,
            workspace_id=first_container.workspace_id,
            name=first_container.name,
            status=first_container.status.value,
        )

    scheduled = functions.schedule_due_retries(now=datetime.now(UTC))

    assert len(scheduled) == 1
    assert len(scheduler.requests) == 2
    assert scheduler.requests[1].container_id != first_container_id
    assert isolated_services.containers.get(first_container_id).status is ContainerStatus.Failed
