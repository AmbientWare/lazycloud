from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from shared.task_context import task_context
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
        # Per request, not per process. Two overlapping requests each set the
        # identity the SDK reads, and with a process global the second one wins
        # for both — so a call spawned by the first is recorded as a child of
        # the second, and the first's restore on the way out clears an identity
        # the second is still using.
        try:
            with task_context(task_id):
                await self.app(scope, receive, send)
        except Exception:
            lifecycle.status = TaskStatus.Failed
            raise
        finally:
            lifecycle.finished_at = time.time()


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
