from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from database.records.apps import StubRecord
from pydantic import Field
from shared.containers import ContainerRecord
from shared.contracts import ContractModel
from shared.deployments import StubKind
from shared.tasks import TaskStatus

from execution.services import ExecutionServices
from execution.taskqueues.service import TaskQueuePreemptedResult


class PreemptedContainerResult(ContractModel):
    task_id: str
    container_id: str
    workload_kind: StubKind
    status: TaskStatus
    changed: bool = False
    retry_scheduled: bool = False
    stale_attempt: bool = False
    queue_message_released: bool = False
    queue_message_acknowledged: bool = False
    details: dict[str, bool] = Field(default_factory=dict)


class PreemptionStubReader(Protocol):
    def get_stub(
        self,
        stub_id_or_name: str,
        *,
        workspace: str | None = None,
    ) -> StubRecord: ...


class PreemptedTaskQueueControl(Protocol):
    def task_queue_preempted(
        self,
        *,
        stub_id: str,
        task_id: str,
        container_id: str,
        exit_code: int | None = None,
        error: str = "task queue container was preempted",
    ) -> TaskQueuePreemptedResult: ...


class PreemptedContainerControl(Protocol):
    def preempted(
        self,
        container: ContainerRecord,
        *,
        exit_code: int,
    ) -> PreemptedContainerResult: ...


@dataclass(frozen=True, slots=True)
class PreemptedContainerService:
    services: ExecutionServices
    stubs: PreemptionStubReader
    task_queues: PreemptedTaskQueueControl

    def preempted(
        self,
        container: ContainerRecord,
        *,
        exit_code: int,
    ) -> PreemptedContainerResult:
        task_id = container.task_id or ""
        if not task_id:
            raise ValueError("preempted container has no task outcome to reconcile")
        task = self.services.tasks.get(task_id)
        stub_id = task.stub_id or container.stub_id or ""
        if not stub_id:
            raise ValueError("preempted task has no workload identity")
        stub = self.stubs.get_stub(stub_id)
        if stub.kind is StubKind.TaskQueue:
            queue = self.task_queues.task_queue_preempted(
                stub_id=stub.id,
                task_id=task.id,
                container_id=container.id,
                exit_code=exit_code,
            )
            return PreemptedContainerResult(
                task_id=queue.task_id,
                container_id=queue.container_id,
                workload_kind=stub.kind,
                status=queue.status,
                changed=queue.changed,
                retry_scheduled=queue.retry_scheduled,
                stale_attempt=queue.stale_attempt,
                queue_message_released=queue.message_released,
                queue_message_acknowledged=queue.message_acknowledged,
                details={"locks_cleared": queue.locks_cleared},
            )

        retry_allowed = stub.kind in {StubKind.Function, StubKind.CronJob}
        outcome = self.services.tasks.finish_with_retry(
            task.id,
            TaskStatus.Failed,
            container_id=container.id,
            error="container execution was preempted",
            exit_code=exit_code,
            retry_allowed=retry_allowed,
        )
        return PreemptedContainerResult(
            task_id=outcome.task.id,
            container_id=container.id,
            workload_kind=stub.kind,
            status=outcome.task.status,
            changed=outcome.state_changed,
            retry_scheduled=outcome.retry_decision.should_retry,
            stale_attempt=not outcome.state_changed
            and outcome.retry_decision.reason
            == "completion does not own the active task container",
        )


__all__ = [
    "PreemptedContainerControl",
    "PreemptedContainerResult",
    "PreemptedContainerService",
    "PreemptedTaskQueueControl",
    "PreemptionStubReader",
]
