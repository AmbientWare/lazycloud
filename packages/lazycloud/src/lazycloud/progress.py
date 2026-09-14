from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime

from shared.http.task_progress import (
    PENDING_NOTICE_DELAY_SECONDS,
    TaskPendingProgress,
    TaskPendingReason,
)
from shared.timestamps import utc_now

from lazycloud.terminal import Terminal, TerminalStep

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


@dataclass
class PendingProgressReporter:
    terminal: Terminal | None = None
    step: TerminalStep | None = None
    _last: tuple[TaskPendingReason, datetime, str] | None = None
    _displayed: tuple[TaskPendingReason, datetime, str] | None = None

    def update(self, task_id: str, pending: TaskPendingProgress | None) -> None:
        key = (pending.reason, pending.since, pending.message) if pending else None
        if key != self._last:
            self._last = key
            callback = _callback.get()
            if callback is not None:
                callback(task_id, pending)
        if pending is None:
            if self._displayed is not None:
                self._report(task_id, None)
            self._displayed = None
            return
        elapsed = (utc_now() - pending.pending_since).total_seconds()
        if elapsed < PENDING_NOTICE_DELAY_SECONDS or key == self._displayed:
            return
        self._displayed = key
        self._report(task_id, pending)

    def _report(self, task_id: str, pending: TaskPendingProgress | None) -> None:
        if self.step is not None:
            self.step.pending_progress(task_id, pending)
        elif self.terminal is not None:
            self.terminal.pending_progress(task_id, pending)


__all__ = ["PendingProgressCallback", "TaskPendingProgress", "TaskPendingReason", "progress"]
