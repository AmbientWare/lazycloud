"""Job progress: what the GPU function records and what the browser is told."""

from enum import StrEnum

from pydantic import BaseModel


class JobStage(StrEnum):
    QUEUED = "queued"
    GENERATING = "generating"
    DONE = "done"
    FAILED = "failed"


FINISHED_STAGES = frozenset({JobStage.DONE, JobStage.FAILED})


class JobProgress(BaseModel):
    """Written to the progress map by the GPU function, keyed by its task ID."""

    stage: JobStage
    completed_steps: int
    total_steps: int
    error: str | None = None


class JobEvent(BaseModel):
    """One WebSocket message to the browser."""

    stage: JobStage
    message: str
    completed_steps: int = 0
    total_steps: int = 0


def describe_job(
    progress: JobProgress | None, *, task_status: str, pending_message: str | None
) -> JobEvent:
    """Combine the function's own progress with the platform's view of its task.

    The task status covers what the function cannot report itself: waiting for
    a GPU, loading the model, and failures that stop it before it writes.
    """
    if progress is not None and progress.stage in FINISHED_STAGES:
        message = "Done" if progress.stage is JobStage.DONE else progress.error or "Failed"
        return JobEvent(
            stage=progress.stage,
            message=message,
            completed_steps=progress.completed_steps,
            total_steps=progress.total_steps,
        )
    if task_status in ("failed", "cancelled"):
        return JobEvent(stage=JobStage.FAILED, message=f"The generation task {task_status}")
    if progress is None:
        return JobEvent(stage=JobStage.QUEUED, message=pending_message or "Starting")
    return JobEvent(
        stage=JobStage.GENERATING,
        message=f"Step {progress.completed_steps} of {progress.total_steps}",
        completed_steps=progress.completed_steps,
        total_steps=progress.total_steps,
    )
