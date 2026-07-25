from __future__ import annotations

from shared.bytes_transport import decode_bytes
from shared.http.base import HttpModel
from shared.tasks import TaskStatus


class StartTaskRequest(HttpModel):
    task_id: str
    container_id: str = ""


class StartTaskResponse(HttpModel):
    task_id: str = ""


class AppendTaskLogRequest(HttpModel):
    task_id: str
    stream: str = "stdout"
    message: str = ""


class AppendTaskLogResponse(HttpModel):
    task_id: str = ""


class EndTaskRequest(HttpModel):
    task_id: str
    task_duration: float = 0
    task_status: TaskStatus = TaskStatus.Complete
    container_id: str = ""
    container_hostname: str = ""
    keep_warm_seconds: float = 0
    result_base64: str = ""

    def result_bytes(self) -> bytes:
        return decode_bytes(self.result_base64)


class EndTaskResponse(HttpModel):
    task_status: TaskStatus | None = None
    final_status: TaskStatus | None = None
    retry_scheduled: bool = False
    attempt_number: int = 0
    max_attempts: int = 1


__all__ = [
    "AppendTaskLogRequest",
    "AppendTaskLogResponse",
    "EndTaskRequest",
    "EndTaskResponse",
    "StartTaskRequest",
    "StartTaskResponse",
]
