from uuid import UUID

from models.statuses import TaskStatus

from backend.prefect_app import get_task_result
from backend.services.monitoring.base import BaseMonitor


class TaskMonitor(BaseMonitor[tuple[TaskStatus, str]]):
    """Monitors Prefect task status."""

    def __init__(self, task_id: UUID, callback=None):
        super().__init__("Task Monitor", str(task_id), callback)
        self.task_id = task_id

    async def _task(self) -> tuple[TaskStatus, str]:
        status, message = await get_task_result(self.task_id)

        # NOTE: we auto-stop monitoring when task reaches terminal state
        # this can be used as a method wait for task completion on client side
        if status in (TaskStatus.COMPLETED, TaskStatus.ERROR):
            self._running = False

        return (status, message or "")
