from typing import Dict, Any
import time
from enum import Enum

from lazycloud_cli.api.base import BaseAPI


class TaskStatus(str, Enum):
    QUEUED = "queued"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"


class TasksAPI(BaseAPI):
    def __init__(self):
        super().__init__("tasks")

    def get_task_status(self, task_id: str) -> Dict[str, Any]:
        """Get the status of a task"""
        return self._get(task_id)

    def wait_for_task_completion(
        self, task_id: str, poll_interval: int = 2
    ) -> Dict[str, Any]:
        """Poll a task until it completes or fails"""

        while True:
            task_status = self.get_task_status(task_id)
            status = task_status.get("status")

            if status == "completed":
                return task_status
            elif status == "failed":
                error = task_status.get("error", "Unknown error")
                raise Exception(f"Task failed: {error}")

            # Task still in progress, wait and check again
            time.sleep(poll_interval)


tasks_api = TasksAPI()
