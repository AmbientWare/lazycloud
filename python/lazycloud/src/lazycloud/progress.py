from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import TYPE_CHECKING

from shared.http.task_progress import (
    TaskPendingProgress,
    TaskPendingReason,
)

if TYPE_CHECKING:
    pass

PendingProgressCallback = Callable[[str, TaskPendingProgress | None], None]
_callback: ContextVar[PendingProgressCallback | None] = ContextVar("pending_progress", default=None)


@contextmanager
def progress(callback: PendingProgressCallback) -> Iterator[None]:
    """Observe pending progress for calls and task waits in this context.

    The callback receives the task id and current progress when its reason changes.
    A None update clears a previous pending notice. This does not enable terminal output.
    """
    token = _callback.set(callback)
    try:
        yield
    finally:
        _callback.reset(token)


__all__ = ["PendingProgressCallback", "TaskPendingProgress", "TaskPendingReason", "progress"]
