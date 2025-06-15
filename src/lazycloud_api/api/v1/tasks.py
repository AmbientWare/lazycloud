from pydantic import BaseModel
from enum import Enum
from typing import Any, Optional
from fastapi import APIRouter, HTTPException
from lazycloud_api.celery_app import app as celery_app

tasks_router = APIRouter(prefix="/tasks", tags=["tasks"])


class TaskStatus(str, Enum):
    QUEUED = "queued"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"


class TaskResponse(BaseModel):
    task_id: str
    status: TaskStatus
    message: str


class TaskStatusResponse(BaseModel):
    task_id: str
    status: TaskStatus
    message: str
    result: Optional[Any] = None
    error: Optional[str] = None
    progress: Optional[str] = None


@tasks_router.get("/{task_id}")
async def get_task_status(task_id: str) -> TaskStatusResponse:
    """Get the status of a Celery task"""
    try:
        # Get task result from Celery
        result = celery_app.AsyncResult(task_id)

        if result.state == "PENDING":
            return TaskStatusResponse(
                task_id=task_id,
                status=TaskStatus.QUEUED,
                message="Task is queued and waiting to be processed",
            )
        elif result.state == "STARTED":
            return TaskStatusResponse(
                task_id=task_id,
                status=TaskStatus.IN_PROGRESS,
                message="Task is currently being processed",
            )
        elif result.state == "SUCCESS":
            return TaskStatusResponse(
                task_id=task_id,
                status=TaskStatus.COMPLETED,
                message="Task completed successfully",
                result=result.result,
            )
        elif result.state == "FAILURE":
            return TaskStatusResponse(
                task_id=task_id,
                status=TaskStatus.FAILED,
                message="Task failed",
                error=str(result.info),
            )
        else:
            return TaskStatusResponse(
                task_id=task_id,
                status=TaskStatus.IN_PROGRESS,
                message=f"Task state: {result.state}",
            )

    except Exception as e:
        raise HTTPException(status_code=404, detail=f"Task not found: {str(e)}")
