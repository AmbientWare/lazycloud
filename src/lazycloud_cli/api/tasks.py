from lazycloud_cli.api.base import BaseAPI
from lazycloud_cli.api.base_sse import SSEClient
from shared.models.statuses import TaskStatus
from shared.responses.tasks import TaskStatusResponse


class TasksAPI(BaseAPI):
    def __init__(self):
        super().__init__("tasks")
        self._sse_client = SSEClient()

    def get_task_status(self, task_id: str) -> TaskStatusResponse:
        """Get the status of a task"""
        response = self._get(path=f"/{task_id}")
        return TaskStatusResponse(**response)

    async def stream_task_status(self, task_id: str) -> TaskStatusResponse:
        """Stream task updates until completion or failure."""
        result = None

        def handle_event(event_type: str, data: dict) -> None:
            nonlocal result
            if event_type == "status":
                status = TaskStatus(data["status"])
                message = data.get("message", "")
                result = TaskStatusResponse(
                    task_id=task_id,
                    status=status,
                    message=message,
                )

        def handle_error(error: Exception) -> None:
            nonlocal result
            result = TaskStatusResponse(
                task_id=task_id,
                status=TaskStatus.ERROR,
                message=str(error),
            )

        await self._sse_client.stream(
            path=f"/tasks/{task_id}/stream",
            on_event=handle_event,
            on_error=handle_error,
        )
        return result or TaskStatusResponse(
            task_id=task_id,
            status=TaskStatus.ERROR,
            message="Stream ended without result",
        )
