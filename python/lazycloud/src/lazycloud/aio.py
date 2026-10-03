from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any, TypeVar

# asyncio loads with the first async call; user modules import this one at
# every container start.
if TYPE_CHECKING:
    from asyncio import AbstractEventLoop

R = TypeVar("R")


def run_sync(awaitable: Awaitable[R], loop: AbstractEventLoop | None = None) -> R:
    selected_loop = loop or _event_loop()
    if selected_loop.is_running():
        msg = "run_sync cannot be used while the selected event loop is already running"
        raise RuntimeError(msg)
    return selected_loop.run_until_complete(awaitable)


async def to_thread(func: Callable[..., R], *args: Any, **kwargs: Any) -> R:
    import asyncio

    return await asyncio.to_thread(func, *args, **kwargs)


def _event_loop() -> AbstractEventLoop:
    import asyncio

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
