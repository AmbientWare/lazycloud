from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any, Protocol, TypeVar

from pydantic import BaseModel
from shared.http.errors import HttpResponseDecodeError
from shared.http.taskqueues import (
    DEFAULT_TASK_QUEUE_SERVE_TIMEOUT_SECONDS,
    StartTaskQueueServeRequest,
    StartTaskQueueServeResponse,
    TaskQueueCompleteBody,
    TaskQueueCompleteResponse,
    TaskQueueMonitorRequest,
    TaskQueueMonitorResponse,
    TaskQueuePopRequest,
    TaskQueuePopResponse,
    TaskQueuePutBody,
    TaskQueuePutResponse,
    TaskQueueSerializedInvocation,
)
from shared.http_transport import HttpChannel


class TaskQueueControlChannel(Protocol):
    def post(self, path: str, payload: dict[str, Any] | None = None) -> Any: ...


ResponseT = TypeVar("ResponseT", bound=BaseModel)


@dataclass
class TaskQueueControlClient:
    channel: TaskQueueControlChannel

    @classmethod
    def from_endpoint(
        cls,
        endpoint: str,
        *,
        token: str | None = None,
        timeout_seconds: float = 10.0,
    ) -> TaskQueueControlClient:
        return cls(
            channel=HttpChannel(endpoint=endpoint, token=token, timeout_seconds=timeout_seconds)
        )

    def put(self, stub_id: str, payload: bytes) -> TaskQueuePutResponse:
        body = TaskQueuePutBody(
            stub_id=stub_id,
            invocation=TaskQueueSerializedInvocation.from_bytes(payload),
        )
        return _validate_response(
            TaskQueuePutResponse,
            self.channel.post("/api/v1/taskqueues/put", body.model_dump(mode="json")),
        )

    def pop(self, stub_id: str, container_id: str) -> TaskQueuePopResponse:
        request = TaskQueuePopRequest(stub_id=stub_id, container_id=container_id)
        return _validate_response(
            TaskQueuePopResponse,
            self.channel.post("/api/v1/taskqueues/pop", request.model_dump(mode="json")),
        )

    def monitor_once(
        self,
        task_id: str,
        stub_id: str,
        *,
        container_id: str = "",
    ) -> TaskQueueMonitorResponse:
        return _validate_response(
            TaskQueueMonitorResponse,
            self.channel.post(
                "/api/v1/taskqueues/monitor",
                TaskQueueMonitorRequest(
                    task_id=task_id,
                    stub_id=stub_id,
                    container_id=container_id,
                ).model_dump(mode="json"),
            ),
        )

    def complete(self, body: TaskQueueCompleteBody) -> TaskQueueCompleteResponse:
        return _validate_response(
            TaskQueueCompleteResponse,
            self.channel.post("/api/v1/taskqueues/complete", body.model_dump(mode="json")),
        )

    def start_serve(
        self,
        stub_id: str,
        *,
        timeout: int = DEFAULT_TASK_QUEUE_SERVE_TIMEOUT_SECONDS,
    ) -> StartTaskQueueServeResponse:
        request = StartTaskQueueServeRequest(stub_id=stub_id, timeout=timeout)
        return _validate_response(
            StartTaskQueueServeResponse,
            self.channel.post("/api/v1/taskqueues/serve", request.model_dump(mode="json")),
        )

    def task_queue_monitor(
        self,
        request: TaskQueueMonitorRequest,
    ) -> Iterator[TaskQueueMonitorResponse]:
        yield _validate_response(
            TaskQueueMonitorResponse,
            self.channel.post("/api/v1/taskqueues/monitor", request.model_dump(mode="json")),
        )


def _validate_response(model: type[ResponseT], value: object) -> ResponseT:
    try:
        return model.model_validate(value)
    except ValueError as exc:
        raise HttpResponseDecodeError("task queue control returned an invalid response") from exc


__all__ = [
    "TaskQueueControlChannel",
    "TaskQueueControlClient",
]
