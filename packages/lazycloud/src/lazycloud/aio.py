from __future__ import annotations

import asyncio
from asyncio import AbstractEventLoop
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

R = TypeVar("R")


def run_sync(awaitable: Awaitable[R], loop: AbstractEventLoop | None = None) -> R:
    selected_loop = loop or _event_loop()
    if selected_loop.is_running():
        msg = "run_sync cannot be used while the selected event loop is already running"
        raise RuntimeError(msg)
    return selected_loop.run_until_complete(awaitable)


async def to_thread(func: Callable[..., R], *args: Any, **kwargs: Any) -> R:
    return await asyncio.to_thread(func, *args, **kwargs)


def _event_loop() -> AbstractEventLoop:
    try:
        return asyncio.get_event_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        return loop


__all__ = [
    "run_sync",
    "to_thread",
]
