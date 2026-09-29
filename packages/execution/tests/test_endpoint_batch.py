from __future__ import annotations

import asyncio
from collections.abc import Sequence

import pytest
from execution.endpoints.batching import EndpointBatch


@pytest.mark.anyio
async def test_disconnecting_twice_settles_accepted_work_without_losing_another_request() -> None:
    started = asyncio.Event()
    commit = asyncio.Event()
    abandoned: list[str] = []

    async def execute(requests: Sequence[str]) -> Sequence[str]:
        started.set()
        await commit.wait()
        return requests

    async def abandon(result: str) -> None:
        abandoned.append(result)

    batch = EndpointBatch(execute, abandon, lambda: None)
    disconnected = asyncio.create_task(batch.submit("disconnected"))
    neighbour = asyncio.create_task(batch.submit("neighbour"))
    await started.wait()
    disconnected.cancel()
    await asyncio.sleep(0)
    disconnected.cancel()
    commit.set()

    with pytest.raises(asyncio.CancelledError):
        await disconnected
    assert await asyncio.wait_for(neighbour, timeout=1) == "neighbour"
    assert abandoned == ["disconnected"]
