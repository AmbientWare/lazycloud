from __future__ import annotations

from dataclasses import dataclass

from database.repositories.apps import StubRepository
from database.repositories.execution import TaskRepository
from database.repositories.orchestration import AutoscalingTargetRepository
from database.types import DatabaseSession
from shared.autoscaler_state import autoscaler_target_kind
from shared.errors import InvalidInputError
from shared.tasks import Task


@dataclass(frozen=True, slots=True)
class TaskClaimReleaseService:
    session: DatabaseSession

    def release(self, task_id: str, *, container_id: str | None) -> Task | None:
        task = TaskRepository(self.session).release_claim(task_id, container_id=container_id)
        if task is None or task.claimable_at is None or not task.stub_id:
            return task
        if not task.workspace_id:
            raise InvalidInputError("claimable task has no workspace")
        stub = StubRepository(self.session).records.get(
            task.stub_id, workspace_id=task.workspace_id
        )
        if stub is not None and (kind := autoscaler_target_kind(stub.kind)) is not None:
            # The last serving container can retire the autoscaling target while
            # this task is still claimed. Commit its renewed demand with the claim.
            AutoscalingTargetRepository(self.session).activate(
                stub_id=stub.id, workspace_id=task.workspace_id, target_kind=kind
            )
        return task

    def release_container(
        self, container_id: str, *, except_task_id: str | None = None
    ) -> list[Task]:
        released: list[Task] = []
        for held in TaskRepository(self.session).list_inflight_for_container(container_id):
            if held.id == except_task_id:
                continue
            task = self.release(held.id, container_id=container_id)
            if task is not None:
                released.append(task)
        return released
