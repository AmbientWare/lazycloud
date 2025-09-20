import time

from lazycloud_cli.api.base import BaseAPI
from shared.models.statuses import TaskStatus
from shared.models.tasks import TaskStatusResponse


class TasksAPI(BaseAPI):
    def __init__(self):
        super().__init__("tasks")

    def get_task_status(self, task_id: str) -> TaskStatusResponse:
        """Get the status of a task"""
        response = self._get(task_id)
        return TaskStatusResponse(**response)

    def wait_for_task_completion(
        self, task_id: str, poll_interval: int = 2
    ) -> TaskStatusResponse:
        """Poll a task until it completes or fails"""

        while True:
            task_response = self.get_task_status(task_id)

            if task_response.status == TaskStatus.COMPLETED:
                return task_response
            elif task_response.status == TaskStatus.ERROR:
                error = task_response.error or "Unknown error"
                raise Exception(f"Task failed: {error}")

            # Task still in progress, wait and check again
            time.sleep(poll_interval)
