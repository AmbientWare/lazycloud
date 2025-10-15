from uuid import UUID

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse

from lazycloud_api.api.v1.streaming_utils import create_sse_stream
from lazycloud_api.prefect_app import get_task_result
from lazycloud_api.services.monitoring import TaskMonitor
from shared.responses.tasks import TaskStatusResponse

tasks_router = APIRouter(prefix="/tasks", tags=["tasks"])


@tasks_router.get("/{task_id}")
async def get_task_status(task_id: UUID) -> TaskStatusResponse:
    """Get the status of a Prefect task"""
    try:
        # Get task result from Prefect
        status, message = await get_task_result(task_id)

        return TaskStatusResponse(
            task_id=task_id,
            status=status,
            message=message,
        )

    except Exception as e:
        raise HTTPException(status_code=404, detail=f"Task not found: {str(e)}")


@tasks_router.get("/{task_id}/stream")
async def stream_task_status(task_id: UUID):
    """Stream real-time task status updates."""
    monitor = TaskMonitor(task_id=task_id, callback=None)

    return StreamingResponse(
        create_sse_stream(
            monitor,
            event_type="status",
            format_data=lambda data: {"status": data[0].value, "message": data[1]},
            stream_id=f"task/{task_id}",
        ),
        media_type="text/event-stream",
    )
