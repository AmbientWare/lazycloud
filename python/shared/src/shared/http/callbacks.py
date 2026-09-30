from __future__ import annotations

from datetime import datetime

from pydantic import JsonValue

from shared.http.base import HttpModel
from shared.tasks import TaskStatus


class TaskCallbackBody(HttpModel):
    task_id: str
    root_task_id: str
    status: TaskStatus
    attempt_number: int
    max_attempts: int
    retry_scheduled: bool
    data: JsonValue = None
    error: str | None = None
    finished_at: datetime | None = None


__all__ = ["TaskCallbackBody"]
