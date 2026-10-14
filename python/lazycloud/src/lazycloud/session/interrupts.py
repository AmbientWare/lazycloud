"""Requests that run to completion through Ctrl-C."""

from __future__ import annotations

import signal
import threading
from collections.abc import Callable
from typing import TypeVar

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


__all__ = ["uninterrupted"]
