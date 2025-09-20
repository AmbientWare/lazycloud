from uuid import UUID

from fastapi import APIRouter, HTTPException

from lazycloud_api.prefect_app import get_task_result
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
