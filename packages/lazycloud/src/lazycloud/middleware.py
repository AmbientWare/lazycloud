from __future__ import annotations

import os
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from shared.tasks import TaskStatus

ASGIReceive = Callable[[], Awaitable[dict[str, Any]]]
ASGISend = Callable[[dict[str, Any]], Awaitable[None]]
ASGIApp = Callable[[dict[str, Any], ASGIReceive, ASGISend], Awaitable[None]]


@dataclass(slots=True)
class TaskLifecycleData:
    task_id: str
    status: TaskStatus = TaskStatus.Complete
    result: Any = None
    started_at: float = 0.0
    finished_at: float = 0.0
    override_callback_url: str | None = None


class TaskLifecycleMiddleware:
    def __init__(self, app: ASGIApp, *, task_header: str = "x-task-id") -> None:
        self.app = app
        self.task_header = task_header.lower().encode()

    async def __call__(
        self,
        scope: dict[str, Any],
        receive: ASGIReceive,
        send: ASGISend,
    ) -> None:
        if scope.get("type") not in {"http", "websocket"}:
            await self.app(scope, receive, send)
            return
        task_id = _header(scope, self.task_header)
        if not task_id:
            await self.app(scope, receive, send)
            return
        lifecycle = TaskLifecycleData(task_id=task_id, started_at=time.time())
        scope.setdefault("state", {})["task_lifecycle_data"] = lifecycle
        previous_task_id = os.environ.get("TASK_ID")
        os.environ["TASK_ID"] = task_id
        try:
            await self.app(scope, receive, send)
        except Exception:
            lifecycle.status = TaskStatus.Failed
            raise
        finally:
            lifecycle.finished_at = time.time()
            if previous_task_id is None:
                os.environ.pop("TASK_ID", None)
            else:
                os.environ["TASK_ID"] = previous_task_id


WebsocketTaskLifecycleMiddleware = TaskLifecycleMiddleware


def _header(scope: dict[str, Any], name: bytes) -> str:
    for key, value in scope.get("headers", ()):
        if key.lower() == name:
            return value.decode("utf-8")
    return ""


__all__ = [
    "ASGIApp",
    "ASGIReceive",
    "ASGISend",
    "TaskLifecycleData",
    "TaskLifecycleMiddleware",
    "WebsocketTaskLifecycleMiddleware",
]
