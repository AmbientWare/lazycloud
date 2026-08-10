from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime

from database.repositories.execution import TaskRepository
from database.repositories.identity import WorkspaceMemberRepository
from database.repositories.orchestration import (
    ContainerRepository,
    MachineRepository,
    WorkerRepository,
)
from observability.events import EventService
from observability.workspace_changes import WorkspaceChangePublisher
from shared.containers import ContainerRecord, ContainerStatus
from shared.errors import ConflictError, InvalidInputError, NotFoundError
from shared.events import EventLevel
from shared.http.workspace_changes import WorkspaceChangeTopic, WorkspaceChangeType
from shared.scheduling import SchedulerWorkerRequest
from shared.tasks import Task, TaskStatus, is_terminal_task_status
from shared.timestamps import utc_now

from execution.containers.runtime_state import ContainerRuntimeStateRepository
from execution.context import ExecutionContext

LOGGER = logging.getLogger(__name__)


@dataclass(slots=True)
class ContainerSchedulingPersistenceService:
    context: ExecutionContext
    events: EventService
    workspace_changes: WorkspaceChangePublisher
    runtime_state: ContainerRuntimeStateRepository | None = None

    def assign_runtime(
        self,
        *,
        container_id: str,
        workspace_id: str,
        runtime_worker_id: str,
        runtime_machine_id: str,
        compute_worker_id: str | None = None,
        compute_machine_id: str | None = None,
    ) -> None:
        if not runtime_worker_id:
            raise InvalidInputError("runtime worker id is required")
        if (compute_worker_id is None) != (compute_machine_id is None):
            raise InvalidInputError(
                "compute worker and machine assignments must be provided together"
            )

        changed = False
        with self.context.database.session() as session:
            containers = ContainerRepository(session)
            container = containers.get_across_workspaces(container_id)
            if container is None:
                raise NotFoundError(f"container not found: {container_id}")
            if container.workspace_id != workspace_id:
                raise ConflictError(
                    f"container {container_id} does not belong to assignment workspace"
                )

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
            if any(getattr(container, field) != value for field, value in update.items()):
                container = containers.upsert(container.model_copy(update=update))
                changed = True

        if changed:
            self._publish_container_change(container)

    def clear_runtime_assignment(
        self,
        *,
        container_id: str,
        runtime_worker_id: str,
    ) -> None:
        with self.context.database.session() as session:
            containers = ContainerRepository(session)
            container = containers.get_across_workspaces(container_id)
            if container is None or container.runtime_worker_id != runtime_worker_id:
                return
            container = containers.upsert(
                container.model_copy(
                    update={
                        "runtime_worker_id": "",
                        "runtime_machine_id": "",
                    }
                )
            )
        self._publish_container_change(container)

    def _release_runtime_state(self, container: ContainerRecord) -> None:
        """Best effort, for the same reason as the container service: a failed
        cache write must not stop a container being recorded as failed."""
        if self.runtime_state is None or not container.stub_id:
            return
        try:
            self.runtime_state.release(
                workspace_id=container.workspace_id,
                stub_id=container.stub_id,
                container_id=container.id,
            )
        except Exception:
            LOGGER.warning(
                "releasing container runtime state failed",
                exc_info=True,
                extra={"container_id": container.id},
            )

    def mark_scheduling_failed(
        self,
        request: SchedulerWorkerRequest,
        reason: str,
        *,
        now: datetime | None = None,
    ) -> None:
        current_time = now or utc_now()
        task: Task | None = None
        with self.context.database.session() as session:
            container = ContainerRepository(session).get_across_workspaces(request.container_id)
            if container is None:
                return
            container.status = ContainerStatus.Failed
            container.exit_code = 1
            container.finished_at = container.finished_at or current_time
            self._release_runtime_state(container)
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
        self.events.emit(
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
