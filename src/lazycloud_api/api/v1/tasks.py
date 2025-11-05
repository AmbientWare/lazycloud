from uuid import UUID

from fastapi import APIRouter, HTTPException
from sse_starlette.sse import EventSourceResponse

from lazycloud_api.api.utils import create_sse_stream_with_subscription
from lazycloud_api.prefect_app import get_task_result
from lazycloud_api.services.monitoring.monitor_config import TaskMonitorConfig
from shared.models.monitoring import StreamEventType
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
    config = TaskMonitorConfig(task_id=task_id)

    return EventSourceResponse(
        create_sse_stream_with_subscription(
            config=config,
            event_type=StreamEventType.STATUS,
            format_data=lambda data: {"status": data[0].value, "message": data[1]},
            stream_id=f"task/{task_id}",
        )
    )
