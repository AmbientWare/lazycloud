from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import Field, model_validator

from shared.bytes_transport import EncodedBytesBody, decode_bytes
from shared.http.base import HttpModel
from shared.tasks import TaskStatus

DEFAULT_TASK_QUEUE_SERVE_TIMEOUT_SECONDS = 600


class TaskQueueInvocationEnvelope(HttpModel):
    args: tuple[Any, ...]
    kwargs: dict[str, Any]


class TaskQueueSerializedInvocation(EncodedBytesBody):
    @model_validator(mode="after")
    def validate_base64(self) -> TaskQueueSerializedInvocation:
        if not self.value_base64:
            raise ValueError("task queue invocation must not be empty")
        try:
            decode_bytes(self.value_base64)
        except ValueError as exc:
            raise ValueError("task queue invocation must be valid base64") from exc
        return self


class TaskQueueTaskMessage(HttpModel):
    workspace_name: str
    stub_id: str
    task_id: str
    invocation: TaskQueueSerializedInvocation


class TaskQueuePutBody(HttpModel):
    stub_id: str
    invocation: TaskQueueSerializedInvocation


class TaskQueuePutResponse(HttpModel):
    task_id: str = ""


class TaskQueuePopRequest(HttpModel):
    stub_id: str
    container_id: str


class TaskQueuePopResponse(EncodedBytesBody):
    @property
    def task_msg(self) -> bytes:
        return self.bytes_value()

    @classmethod
    def from_bytes(cls, value: bytes) -> TaskQueuePopResponse:
        return cls(value_base64=EncodedBytesBody.from_bytes(value).value_base64)


class TaskQueueStateResponse(HttpModel):
    queue_depth: int = Field(default=0, ge=0)
    oldest_pending_at: datetime | None = None
    active_consumers: int = Field(default=0, ge=0)
    busy_consumers: int = Field(default=0, ge=0)
    available_consumers: int = Field(default=0, ge=0)


class TaskQueueCompleteBody(EncodedBytesBody):
    task_id: str
    stub_id: str
    task_duration: float = 0.0
    task_status: TaskStatus = TaskStatus.Complete
    container_id: str = ""
    container_hostname: str = ""
    keep_warm_seconds: float = 0.0
    error: str = ""


class TaskQueueCompleteResponse(HttpModel):
    message: str = ""
    task_status: TaskStatus | None = None
    final_status: TaskStatus | None = None
    retry_scheduled: bool = False
    attempt_number: int = 0
    max_attempts: int = 1


class TaskQueueMonitorRequest(HttpModel):
    task_id: str
    stub_id: str
    container_id: str = ""


class TaskQueueMonitorResponse(HttpModel):
    cancelled: bool = False
    complete: bool = False
    timed_out: bool = False


class StartTaskQueueServeRequest(HttpModel):
    stub_id: str
    timeout: int = Field(default=DEFAULT_TASK_QUEUE_SERVE_TIMEOUT_SECONDS, gt=0)


class StartTaskQueueServeResponse(HttpModel):
    container_id: str = ""


__all__ = [
    "DEFAULT_TASK_QUEUE_SERVE_TIMEOUT_SECONDS",
    "StartTaskQueueServeRequest",
    "StartTaskQueueServeResponse",
    "TaskQueueCompleteBody",
    "TaskQueueCompleteResponse",
    "TaskQueueInvocationEnvelope",
    "TaskQueueMonitorRequest",
    "TaskQueueMonitorResponse",
    "TaskQueuePopRequest",
    "TaskQueuePopResponse",
    "TaskQueuePutBody",
    "TaskQueuePutResponse",
    "TaskQueueSerializedInvocation",
    "TaskQueueStateResponse",
    "TaskQueueTaskMessage",
]
