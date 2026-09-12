from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from datetime import datetime

from database.repositories.billing_ledger import ContainerBillingShapeRepository
from database.repositories.container_scheduling import ContainerSchedulingRepository
from database.repositories.execution import TaskRepository
from database.repositories.identity import WorkspaceMemberRepository
from database.repositories.orchestration import (
    ContainerRepository,
    MachineRepository,
    WorkerRepository,
)
from observability.events import EventService
from observability.workspace_changes import WorkspaceChangePublisher
from shared.billing_quotes import ContainerShape
from shared.containers import ContainerRecord, ContainerStatus
from shared.errors import ConflictError, InvalidInputError, NotFoundError
from shared.events import EventLevel
from shared.http.workspace_changes import WorkspaceChangeTopic, WorkspaceChangeType
from shared.scheduling import SchedulerWorkerRequest
from shared.tasks import Task, TaskStatus, is_terminal_task_status
from shared.timestamps import utc_now

from execution.containers.runtime_state import (
    ContainerRuntimeStateRepository,
    release_container_runtime_state,
)
from execution.context import ExecutionContext
from execution.task_claims import TaskClaimReleaseService

LOGGER = logging.getLogger(__name__)


@dataclass(slots=True)
class ContainerSchedulingPersistenceService:
    context: ExecutionContext
    events: EventService
    workspace_changes: WorkspaceChangePublisher
    runtime_state: ContainerRuntimeStateRepository | None = None

    def record_scheduling_request(self, request: SchedulerWorkerRequest, *, now: datetime) -> bool:
        with self.context.database.session() as session:
            return ContainerSchedulingRepository(session).submit(request, now=now)

    def recoverable_scheduling_requests(
        self, *, now: datetime, limit: int
    ) -> list[SchedulerWorkerRequest]:
        with self.context.database.session() as session:
            return ContainerSchedulingRepository(session).recoverable(now=now, limit=limit)

    def scheduling_request_reconciled(self, container_id: str, *, retry_at: datetime) -> None:
        with self.context.database.session() as session:
            ContainerSchedulingRepository(session).reconciled(container_id, retry_at=retry_at)

    def request_capacity(
        self, request: SchedulerWorkerRequest, *, now: datetime
    ) -> SchedulerWorkerRequest:
        with self.context.database.session() as session:
            return ContainerSchedulingRepository(session).request_capacity(request, now=now)

    def capacity_requests_due(self, *, now: datetime, limit: int) -> list[SchedulerWorkerRequest]:
        with self.context.database.session() as session:
            return ContainerSchedulingRepository(session).capacity_due(now=now, limit=limit)

    def capacity_request_due(
        self, container_id: str, *, now: datetime
    ) -> SchedulerWorkerRequest | None:
        with self.context.database.session() as session:
            return ContainerSchedulingRepository(session).capacity_due_request(
                container_id, now=now
            )

    def record_capacity_attempt(
        self, request: SchedulerWorkerRequest, *, retry_at: datetime
    ) -> None:
        with self.context.database.session() as session:
            ContainerSchedulingRepository(session).record_capacity_attempt(
                request, retry_at=retry_at
            )

    def publish_pending_progress(self, container_id: str) -> None:
        with self.context.database.session() as session:
            container = ContainerRepository(session).get_across_workspaces(container_id)
        if container is not None and container.status is ContainerStatus.Pending:
            self._publish_container_change(container)

    def expired_assignments(
        self, *, before: datetime, limit: int
    ) -> list[tuple[ContainerRecord, datetime]]:
        with self.context.database.session() as session:
            return ContainerSchedulingRepository(session).expired_assignments(
                before=before, limit=limit
            )

    def assign_runtime(
        self,
        *,
        container_id: str,
        workspace_id: str,
        runtime_worker_id: str,
        runtime_machine_id: str,
        assignment_token: str,
        assigned_at: datetime,
        backfill: bool,
        compute_worker_id: str | None = None,
        compute_machine_id: str | None = None,
        shape: ContainerShape | None = None,
    ) -> None:
        if not runtime_worker_id:
            raise InvalidInputError("runtime worker id is required")
        if not assignment_token:
            raise InvalidInputError("assignment token is required")
        if (compute_worker_id is None) != (compute_machine_id is None):
            raise InvalidInputError(
                "compute worker and machine assignments must be provided together"
            )

        with self.context.database.session() as session:
            containers = ContainerRepository(session)
            container = containers.lock_across_workspaces(container_id)
            if container is None:
                raise NotFoundError(f"container not found: {container_id}")
            if container.workspace_id != workspace_id:
                raise ConflictError(
                    f"container {container_id} does not belong to assignment workspace"
                )
            if container.status is not ContainerStatus.Pending or container.runtime_worker_id:
                raise ConflictError("container has already been assigned or stopped")

            if compute_worker_id is not None and compute_machine_id is not None:
                workers = WorkerRepository(session)
                machines = MachineRepository(session)
                worker = workers.get_across_workspaces(compute_worker_id)
                machine = machines.get_across_workspaces(compute_machine_id)
                if worker is None or machine is None:
                    raise NotFoundError("compute worker or machine assignment is unavailable")
                machine_workspace_id = machines.workspace_id(compute_machine_id)
                if workers.workspace_id(compute_worker_id) != machine_workspace_id:
                    raise ConflictError("compute worker and machine belong to different workspaces")
                # Compared by account, not by workspace. A joined machine belongs to
                # the customer and serves every workspace they own, so its own
                # workspace is where its rows live rather than who it may run for.
                members = WorkspaceMemberRepository(session)
                machine_owner = (
                    members.owner(machine_workspace_id) if machine_workspace_id else None
                )
                container_owner = members.owner(workspace_id)
                if (
                    machine_owner is None
                    or container_owner is None
                    or machine_owner.user_id != container_owner.user_id
                ):
                    raise ConflictError(
                        "compute worker or machine does not belong to the assignment's account"
                    )
                if worker.machine_id != compute_machine_id:
                    raise ConflictError("compute worker does not belong to assignment machine")

            update: dict[str, str | None] = {
                "runtime_worker_id": runtime_worker_id,
                "runtime_machine_id": runtime_machine_id,
                "worker_id": compute_worker_id,
                "machine_id": compute_machine_id,
            }
            if shape is not None:
                # Written here because this is where the control plane decides
                # where a container runs: a container that is running has a shape
                # and one that never started has none. Recorded from what was
                # placed rather than from what the worker later reports, and in
                # this transaction so the two cannot disagree.
                billing_shapes = ContainerBillingShapeRepository(session)
                recorded = billing_shapes.shape_for(container_id)
                if recorded is not None:
                    shape = replace(shape, rate_class=recorded.rate_class)
                billing_shapes.record(
                    container_id=container_id,
                    workspace_id=workspace_id,
                    shape=shape,
                )

            container = container.model_copy(update=update)
            ContainerSchedulingRepository(session).record_assignment(
                container, now=assigned_at, token=assignment_token, backfill=backfill
            )

        self._publish_container_change(container)

    def clear_runtime_assignment(
        self,
        *,
        container_id: str,
        runtime_worker_id: str,
        assignment_token: str,
    ) -> bool:
        with self.context.database.session() as session:
            containers = ContainerRepository(session)
            container = containers.lock_across_workspaces(container_id)
            if (
                container is None
                or container.runtime_worker_id != runtime_worker_id
                or container.status is not ContainerStatus.Pending
            ):
                return False
            scheduling = ContainerSchedulingRepository(session)
            if not scheduling.owns_assignment(container_id, token=assignment_token):
                return False
            if not ContainerBillingShapeRepository(session).discard_provisional(container_id):
                return False
            container = container.model_copy(
                update={
                    "runtime_worker_id": "",
                    "runtime_machine_id": "",
                    "worker_id": None,
                    "machine_id": None,
                }
            )
            scheduling.record_assignment(container, now=None, token=None, backfill=False)
        self._publish_container_change(container)
        return True

    def mark_scheduling_failed(
        self,
        request: SchedulerWorkerRequest,
        reason: str,
        *,
        now: datetime | None = None,
    ) -> bool:
        current_time = now or utc_now()
        task: Task | None = None
        with self.context.database.session() as session:
            container = ContainerRepository(session).lock_across_workspaces(request.container_id)
            if (
                container is None
                or container.status is not ContainerStatus.Pending
                or container.runtime_worker_id
            ):
                return False
            container.status = ContainerStatus.Failed
            container.exit_code = 1
            container.finished_at = container.finished_at or current_time
            release_container_runtime_state(self.runtime_state, container)
            ContainerRepository(session).records.upsert(
                container,
                workspace_id=container.workspace_id,
                name=container.name,
                status=container.status.value,
            )
            if container.task_id:
                task = TaskRepository(session).get_across_workspaces(container.task_id)
                if task is not None and not is_terminal_task_status(task.status):
                    task.status = TaskStatus.Failed
                    task.error = reason
                    task.exit_code = 1
                    task.finished_at = task.finished_at or current_time
                    task.kwargs.setdefault("container_id", container.id)
                    TaskRepository(session).upsert(
                        task,
                        workspace_id=container.workspace_id,
                    )
            TaskClaimReleaseService(session).release_container(
                container.id, except_task_id=container.task_id
            )
            self.events.emit_in_session(
                session,
                "container.schedule.failed",
                resource_type="container",
                resource_id=request.container_id,
                message=reason or f"failed to schedule container {request.container_id}",
                level=EventLevel.Error,
                data={"scheduler_failure": True},
                workspace_id=request.workspace_id,
            )
        self._publish_container_change(container)
        if task is not None:
            self._publish_task_change(task)
        return True

    def _publish_container_change(self, container: ContainerRecord) -> None:
        if not container.workspace_id:
            return
        self.workspace_changes.emit_change(
            workspace_id=container.workspace_id,
            topic=WorkspaceChangeTopic.Containers,
            change=WorkspaceChangeType.Updated,
            resource_id=container.id,
            app_id=container.app_id,
            stub_id=container.stub_id,
            task_id=container.task_id,
            container_id=container.id,
        )

    def _publish_task_change(self, task: Task) -> None:
        if not task.workspace_id:
            return
        self.workspace_changes.emit_change(
            workspace_id=task.workspace_id,
            topic=WorkspaceChangeTopic.Tasks,
            change=WorkspaceChangeType.Updated,
            resource_id=task.id,
            app_id=task.app_id,
            deployment_id=task.deployment_id,
            stub_id=task.stub_id,
            task_id=task.id,
            root_task_id=task.root_task_id,
            container_id=task.container_id,
        )


__all__ = ["ContainerSchedulingPersistenceService"]
