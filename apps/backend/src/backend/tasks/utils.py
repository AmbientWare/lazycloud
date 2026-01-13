"""SAQ utility functions for job status tracking."""

from typing import Any
from uuid import UUID

from loguru import logger
from saq import Status

from backend.tasks.queue import get_background_queue

# Import TaskStatus from models
from models.statuses import TaskStatus

# Map SAQ Status to TaskStatus
SAQ_STATUS_MAP: dict[Status, TaskStatus] = {
    Status.NEW: TaskStatus.PENDING,
    Status.QUEUED: TaskStatus.PENDING,
    Status.ACTIVE: TaskStatus.PENDING,
    Status.ABORTING: TaskStatus.PENDING,
    Status.ABORTED: TaskStatus.ERROR,
    Status.FAILED: TaskStatus.ERROR,
    Status.COMPLETE: TaskStatus.COMPLETED,
}


def _transform_error_message(error: str | None) -> str:
    """Transform technical errors to user-friendly messages."""
    if not error:
        return "Job failed without specific error message."

    # Check for common technical errors and provide user-friendly messages
    if "ImportError" in error or "cannot import name" in error:
        return "Deployment failed due to a configuration error. Please contact support."

    error_lower = error.lower()
    if "exceeded quota" in error_lower or (
        "quota" in error_lower and "forbidden" in error_lower
    ):
        return "Resource limit exceeded. Please reduce the number of services, volumes, or upgrade your plan."

    if "deployments.apps" in error or "count/deployments" in error:
        return "Resource limit exceeded. Please reduce the number of services, volumes, or upgrade your plan."

    if "timeout" in error_lower:
        return "Operation timed out. Please try again."

    return error


async def get_task_result(task_run_id: UUID | str) -> tuple[TaskStatus, Any]:
    """Get SAQ job result/status.

    Args:
        task_run_id: The job key (UUID) to look up

    Returns:
        A tuple of (TaskStatus, message)
    """
    job_key = str(task_run_id)
    queue = get_background_queue()

    try:
        job = await queue.job(job_key)

        if job is None:
            # Job not found - might be expired or never existed
            logger.warning(f"Job {job_key} not found in queue")
            return TaskStatus.ERROR, "Job not found"

        status = SAQ_STATUS_MAP.get(job.status, TaskStatus.PENDING)

        if job.status == Status.COMPLETE:
            # Extract message from result if available
            message = "Job completed"
            if isinstance(job.result, dict) and "message" in job.result:
                message = str(job.result["message"])
            elif isinstance(job.result, dict) and "status" in job.result:
                message = f"Job completed: {job.result.get('status', 'success')}"
            return TaskStatus.COMPLETED, message

        if job.status == Status.FAILED:
            error_message = _transform_error_message(job.error)
            return TaskStatus.ERROR, error_message

        if job.status == Status.ABORTED:
            return TaskStatus.ERROR, job.error or "Job aborted"

        # Job is still pending/running
        progress_pct = int((job.progress or 0) * 100)
        if progress_pct > 0:
            return status, f"In progress: {progress_pct}%"

        return status, None

    except Exception as e:
        logger.error(f"Error checking job status for {job_key}: {e}")
        return TaskStatus.ERROR, "Failed to check job status"
