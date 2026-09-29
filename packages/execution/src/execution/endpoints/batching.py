from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field

from anyio import CancelScope


async def complete_before_cancelling[ResultT](operation: Awaitable[ResultT]) -> ResultT:
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


@dataclass(slots=True)
class _Pending[RequestT, ResultT]:
    request: RequestT
    result: asyncio.Future[ResultT]
    started: bool = False


@dataclass(slots=True)
class EndpointBatch[RequestT, ResultT]:
    execute: Callable[[Sequence[RequestT]], Awaitable[Sequence[ResultT | Exception]]]
    abandon: Callable[[ResultT], Awaitable[None]]
    on_idle: Callable[[], None]
    pending: deque[_Pending[RequestT, ResultT]] = field(default_factory=deque, init=False)
    runner: asyncio.Task[None] | None = field(default=None, init=False)

    async def submit(self, request: RequestT) -> ResultT:
        pending: _Pending[RequestT, ResultT] = _Pending(
            request, asyncio.get_running_loop().create_future()
        )
        self.pending.append(pending)
        if self.runner is None:
            self.runner = asyncio.create_task(self._run())
        try:
            return await asyncio.shield(pending.result)
        except asyncio.CancelledError:
            if not pending.started:
                pending.result.cancel()
            else:
                # SQL may have committed after the caller disconnected. Settle its
                # accepted resource before releasing the last owner of that result.
                async def settle() -> None:
                    result = await pending.result
                    await self.abandon(result)

                await complete_before_cancelling(settle())
            raise

    async def _run(self) -> None:
        try:
            while self.pending:
                if len(self.pending) < 64:
                    await asyncio.sleep(0.002)
                batch: list[_Pending[RequestT, ResultT]] = []
                while self.pending and len(batch) < 64:
                    pending = self.pending.popleft()
                    if not pending.result.cancelled():
                        pending.started = True
                        batch.append(pending)
                if not batch:
                    continue
                try:
                    results = await self.execute([item.request for item in batch])
                    if len(results) != len(batch):
                        raise RuntimeError("endpoint batch omitted a result")
                except Exception as exc:
                    for pending in batch:
                        pending.result.set_exception(exc)
                else:
                    for pending, result in zip(batch, results, strict=True):
                        if isinstance(result, Exception):
                            pending.result.set_exception(result)
                        else:
                            pending.result.set_result(result)
        finally:
            self.runner = None
            self.on_idle()
