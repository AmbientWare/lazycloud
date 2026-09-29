from __future__ import annotations

import asyncio
from collections.abc import Awaitable
from typing import TypeVar

from anyio import CancelScope

ResultT = TypeVar("ResultT")


async def complete_before_cancelling(operation: Awaitable[ResultT]) -> ResultT:
    async def complete() -> ResultT:
        return await operation

    task = asyncio.create_task(complete())
    cancelled = None
    with CancelScope(shield=True):
        while True:
            try:
                await asyncio.shield(task)
                break
            except asyncio.CancelledError as exc:
                if task.cancelled():
                    raise
                cancelled = exc
    if cancelled is not None:
        raise cancelled
    return task.result()
