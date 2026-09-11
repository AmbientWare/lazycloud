from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from database.repositories.execution import TaskAttemptRepository
from database.repositories.orchestration import ContainerRepository
from shared.containers import ContainerRecord, ContainerStatus
from shared.http.task_progress import (
    PENDING_PROGRESS_FRESH_SECONDS,
    TaskPendingProgress,
    TaskPendingReason,
)
from shared.scheduling import SchedulerContainerState, SchedulerContainerStatus
from shared.tasks import Task, TaskStatus
from shared.timestamps import utc_now

from execution.context import ExecutionContext


class TaskCapacityReader(Protocol):
    def list_by_stub(self, stub_id: str) -> list[SchedulerContainerState]: ...


@dataclass(slots=True)
class TaskProgressService:
    context: ExecutionContext
    capacity: TaskCapacityReader

    def read(self, tasks: Sequence[Task]) -> dict[str, TaskPendingProgress | None]:
        now = utc_now()
        retried = [
            task.id
            for task in tasks
            if task.invocation is not None
            and task.attempt_number > 0
            and task.status in {TaskStatus.Pending, TaskStatus.Retry}
        ]
        pending_since: dict[str, datetime] = {}
        if retried:
            with self.context.database.session() as session:
                attempts = TaskAttemptRepository(session).latest_for_tasks(retried)
            pending_since = {
                task_id: attempt.finished_at
                for task_id, attempt in attempts.items()
                if attempt.finished_at is not None
            }
        groups = {
            (task.workspace_id, task.stub_id)
            for task in tasks
            if task.invocation is not None
            and task.status is TaskStatus.Pending
            and task.claimable_at is not None
            and task.workspace_id
            and task.stub_id
        }
        containers: dict[tuple[str, str], list[ContainerRecord]] = {}
        states: dict[str, SchedulerContainerState] = {}
        for workspace_id, stub_id in groups:
            with self.context.database.session() as session:
                containers[workspace_id, stub_id] = ContainerRepository(session).list(
                    workspace_id=workspace_id,
                    stub_ids=(stub_id,),
                    statuses=(ContainerStatus.Pending.value, ContainerStatus.Running.value),
                )
            states.update(
                (state.container_id, state)
                for state in self.capacity.list_by_stub(stub_id)
                if state.workspace_id == workspace_id and state.stub_id == stub_id
            )
        return {
            task.id: task_pending_progress(
                task,
                containers=containers.get((task.workspace_id or "", task.stub_id or ""), ()),
                states=states,
                now=now,
                since=pending_since.get(task.id, task.claimable_at or task.created_at),
            )
            for task in tasks
        }


def task_pending_progress(
    task: Task,
    *,
    containers: Sequence[ContainerRecord],
    states: dict[str, SchedulerContainerState],
    now: datetime,
    since: datetime,
) -> TaskPendingProgress | None:
    if task.invocation is None or task.status not in {TaskStatus.Pending, TaskStatus.Retry}:
        return None
    if task.status is TaskStatus.Retry:
        return TaskPendingProgress.for_reason(TaskPendingReason.Retry, since=since, observed_at=now)
    if task.claimable_at is None:
        return TaskPendingProgress.for_reason(
            TaskPendingReason.Dependencies, since=since, observed_at=now
        )
    candidates: list[TaskPendingProgress] = []
    running = False
    for container in containers:
        if container.workspace_id != task.workspace_id or container.stub_id != task.stub_id:
            continue
        state = states.get(container.id)
        if (
            state is None
            or state.workspace_id != task.workspace_id
            or state.stub_id != task.stub_id
        ):
            continue
        if (
            container.status is ContainerStatus.Running
            and state.status is SchedulerContainerStatus.Running
        ):
            running = True
        if (
            container.status is not ContainerStatus.Pending
            or state.status is not SchedulerContainerStatus.Pending
        ):
            continue
        progress = state.pending_progress
        if (
            progress is not None
            and 0 <= (now - progress.observed_at).total_seconds() <= PENDING_PROGRESS_FRESH_SECONDS
        ):
            candidates.append(progress)
    if candidates:
        # A container already starting can serve the queue before a new node arrives.
        priority = {
            TaskPendingReason.StartingContainer: 0,
            TaskPendingReason.ProvisioningCompute: 1,
        }
        progress = min(
            candidates,
            key=lambda item: (priority.get(item.reason, 2), -item.observed_at.timestamp()),
        )
        return progress.model_copy(
            update={"since": max(since, progress.since), "pending_since": since}
        )
    return TaskPendingProgress.for_reason(
        TaskPendingReason.CapacityBusy if running else TaskPendingReason.Queued,
        since=since,
        observed_at=now,
    )
