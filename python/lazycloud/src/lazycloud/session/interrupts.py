"""Requests that run to completion through Ctrl-C."""

from __future__ import annotations

import signal
import threading
from collections.abc import Callable, Sequence
from types import FrameType
from typing import TYPE_CHECKING, TypeVar
from uuid import UUID

if TYPE_CHECKING:
    from lazycloud.session.task import Task

T = TypeVar("T")


def uninterrupted(call: Callable[[], T]) -> tuple[T, KeyboardInterrupt | None]:
    """Run call to completion even through Ctrl-C, returning the interrupt.

    Only the main thread receives SIGINT. It holds the signal from before call
    starts until call returns, so a Ctrl-C never abandons a request the
    server may already have acted on. When call fails after a Ctrl-C, the
    interrupt is raised from its error.
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
    except Exception as error:
        signal.signal(signal.SIGINT, previous)
        interrupt = _replay(previous) if received else None
        if interrupt is None:
            raise
        raise interrupt from error
    except BaseException:
        signal.signal(signal.SIGINT, previous)
        raise
    signal.signal(signal.SIGINT, previous)
    return value, _replay(previous) if received else None


def _replay(handler: Callable[[int, FrameType | None], object]) -> KeyboardInterrupt | None:
    """Deliver a held SIGINT to the handler it was held from."""
    try:
        handler(signal.SIGINT, None)
    except KeyboardInterrupt as exc:
        return exc
    return None


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
        except KeyboardInterrupt as exc:
            interrupted = interrupted or exc
            if isinstance(exc.__cause__, Exception):
                failures.append(exc.__cause__)
            continue
        except Exception as exc:
            failures.append(exc)
            continue
        interrupted = interrupted or again
    if interrupted is not None:
        raise interrupted
    return failures


__all__ = ["cancel_tasks", "uninterrupted"]
