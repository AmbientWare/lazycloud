from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from database.records.apps import StubRecord
from database.repositories.execution import TaskAttemptRepository, TaskRepository
from shared.containers import ContainerRecord
from shared.contracts import ContractModel
from shared.deployments import StubKind
from shared.tasks import TaskStatus

from execution.services import ExecutionServices


class PreemptedContainerResult(ContractModel):
    task_id: str
    container_id: str
    workload_kind: StubKind
    status: TaskStatus
    changed: bool = False
    retry_scheduled: bool = False
    stale_attempt: bool = False


class PreemptionStubReader(Protocol):
    def get_stub(
        self,
        stub_id_or_name: str,
        *,
        workspace: str | None = None,
    ) -> StubRecord: ...


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

    def recover_unsettled(self, *, limit: int = 100) -> list[str]:
        """Settle preemption intents whose inline attempt never completed.

        The terminal container state and this intent commit together, so anything
        still unsettled here is work a control-plane crash left stranded. Settling is
        replay-safe, so an intent that did in fact complete resolves to no change.
        Returns the container ids settled.
        """
        recovered: list[str] = []
        for container in self.services.containers.unsettled_preemptions(limit=limit):
            # Asked unconditionally. A pooled container never carries a task id,
            # so gating on one here would skip precisely the containers whose
            # claims this sweep exists to give back.
            self.preempted(
                container,
                exit_code=container.exit_code if container.exit_code is not None else 0,
            )
            self.services.containers.mark_preemption_settled(container.id)
            recovered.append(container.id)
        return recovered

    def _claimed_task_id(self, container: ContainerRecord) -> str:
        """The invocation this container had taken, if it had taken one.

        Read from the task side because a pooled container is not started for a
        task: `container.task_id` stays empty for its whole life and what it is
        actually running is only recorded by the claim.
        """

        with self.services.context.database.session() as session:
            held = TaskRepository(session).list_inflight_for_container(container.id)
            if held:
                return held[0].id
            # Nothing in flight, which is either a container that was between
            # calls or one already settled. The attempt row tells the two apart,
            # and resolving the settled one is what keeps a second settlement an
            # idempotent no-op rather than a report that nothing ever ran here.
            return TaskAttemptRepository(session).latest_task_id_for_container(container.id)

    def _container_stub_kind(self, container: ContainerRecord) -> StubKind:
        if not container.stub_id:
            return StubKind.Function
        return self.stubs.get_stub(container.stub_id).kind

    def preempted(
        self,
        container: ContainerRecord,
        *,
        exit_code: int,
    ) -> PreemptedContainerResult:
        task_id = container.task_id or self._claimed_task_id(container)
        if not task_id:
            # A pooled container preempted between calls was holding nothing, so
            # there is no outcome to reconcile. That is an ordinary state for a
            # warm container rather than the inconsistency this used to be, when
            # every container existed for exactly one task.
            return PreemptedContainerResult(
                task_id="",
                container_id=container.id,
                workload_kind=self._container_stub_kind(container),
                status=TaskStatus.Pending,
            )
        task = self.services.tasks.get(task_id)
        stub_id = task.stub_id or container.stub_id or ""
        if not stub_id:
            raise ValueError("preempted task has no workload identity")
        stub = self.stubs.get_stub(stub_id)
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
    "PreemptionStubReader",
]
