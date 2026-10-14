"""Requests that run to completion through Ctrl-C."""

from __future__ import annotations

import signal
import threading
from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING, TypeVar
from uuid import UUID

if TYPE_CHECKING:
    from lazycloud.session.task import Task

T = TypeVar("T")


def uninterrupted(call: Callable[[], T]) -> tuple[T, KeyboardInterrupt | None]:
    """Run call to completion even through Ctrl-C, returning the interrupt.

    Only the main thread receives SIGINT. It holds the signal from before call
    starts until call returns, so a Ctrl-C never abandons a request the
    server may already have acted on.
    """
    if threading.current_thread() is not threading.main_thread():
        return call(), None
    previous = signal.getsignal(signal.SIGINT)
    if not callable(previous):
        return call(), None
    received: list[int] = []
    signal.signal(signal.SIGINT, lambda signum, _frame: received.append(signum))
    try:
        value = call()
    finally:
        signal.signal(signal.SIGINT, previous)
    if not received:
        return value, None
    try:
        previous(signal.SIGINT, None)
    except KeyboardInterrupt as exc:
        return value, exc
    return value, None


def cancel_tasks(tasks: Sequence[Task]) -> list[Exception]:
    """Cancel each task, finishing even through repeated Ctrl-C.

    Returns the failures to cancel; an interrupt during cancellation is
    re-raised once every cancel has been attempted.
    """
    failures: list[Exception] = []
    interrupted: KeyboardInterrupt | None = None
    for task in tasks:
        try:
            _, again = uninterrupted(
                lambda task=task: task.client.cancel_task(task.workspace, UUID(task.task_id))
            )
        except Exception as exc:
            failures.append(exc)
            continue
        interrupted = interrupted or again
    if interrupted is not None:
        raise interrupted
    return failures


__all__ = ["cancel_tasks", "uninterrupted"]
