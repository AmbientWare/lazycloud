from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from time import monotonic

from observability.workspace_changes import AsyncWorkloadChangeReader

from execution.endpoints.dispatch import EndpointDispatchTarget


@dataclass(frozen=True, slots=True)
class EndpointCapacitySnapshot:
    targets: Sequence[EndpointDispatchTarget]
    loads: dict[str, int]
    cancelled: frozenset[str] = frozenset()


@dataclass(slots=True)
class _Waiter:
    task_id: str
    result: asyncio.Future[EndpointDispatchTarget | None]


@dataclass(slots=True)
class EndpointCapacity:
    """Share discovery while requests wait; SQL still owns every actual claim."""

    workspace_id: str
    stub_id: str
    concurrency: int
    changes: AsyncWorkloadChangeReader
    discover: Callable[[Sequence[str]], Awaitable[EndpointCapacitySnapshot]]
    waiters: list[_Waiter] = field(default_factory=list, init=False)
    reservations: dict[str, str] = field(default_factory=dict, init=False)
    changed: asyncio.Event = field(default_factory=asyncio.Event, init=False)
    pump: asyncio.Task[None] | None = field(default=None, init=False)

    @asynccontextmanager
    async def reserve(self, task_id: str) -> AsyncIterator[EndpointDispatchTarget | None]:
        waiter = _Waiter(task_id, asyncio.get_running_loop().create_future())
        self.waiters.append(waiter)
        self.changed.set()
        if self.pump is None or self.pump.done():
            self.pump = asyncio.create_task(self._run())
        try:
            yield await waiter.result
        finally:
            self.waiters.remove(waiter)
            self.reservations.pop(task_id, None)
            self.changed.set()
            if not self.waiters and self.pump is not None:
                pump, self.pump = self.pump, None
                pump.cancel()
                await asyncio.gather(pump, return_exceptions=True)

    async def _run(self) -> None:
        try:
            async with self.changes.follow(
                workspace_id=self.workspace_id, stub_id=self.stub_id
            ) as updates:
                listener = asyncio.create_task(self._listen(updates))
                try:
                    next_refresh = 0.0
                    while True:
                        await self.changed.wait()
                        await asyncio.sleep(max(next_refresh - monotonic(), 0.0))
                        self.changed.clear()
                        if listener.done():
                            await listener
                            raise RuntimeError("endpoint capacity notifications closed")
                        pending = [waiter for waiter in self.waiters if not waiter.result.done()]
                        if not pending:
                            continue
                        snapshot = await self.discover([waiter.task_id for waiter in pending])
                        next_refresh = monotonic() + 0.05
                        self._offer(snapshot)
                finally:
                    listener.cancel()
                    await asyncio.gather(listener, return_exceptions=True)
        except Exception as exc:
            for waiter in self.waiters:
                if not waiter.result.done():
                    waiter.result.set_exception(exc)

    async def _listen(self, updates: AsyncIterator[None]) -> None:
        try:
            async for _ in updates:
                self.changed.set()
        finally:
            self.changed.set()

    def _offer(self, snapshot: EndpointCapacitySnapshot) -> None:
        loads = dict(snapshot.loads)
        # An offered slot can still be committing when another snapshot is read.
        # Counting it twice briefly is safe; offering it twice creates claim contention.
        for container_id in self.reservations.values():
            loads[container_id] = loads.get(container_id, 0) + 1
        for waiter in self.waiters:
            if waiter.result.done():
                continue
            if waiter.task_id in snapshot.cancelled:
                waiter.result.set_result(None)
                continue
            targets = (
                target
                for target in snapshot.targets
                if loads.get(target.container_id, 0) < self.concurrency
            )
            target = min(targets, key=lambda item: loads.get(item.container_id, 0), default=None)
            if target is not None:
                loads[target.container_id] = loads.get(target.container_id, 0) + 1
                self.reservations[waiter.task_id] = target.container_id
                waiter.result.set_result(target)
