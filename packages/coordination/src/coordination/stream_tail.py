from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncGenerator, AsyncIterator, Mapping, Sequence
from dataclasses import dataclass, field
from types import TracebackType

from coordination.redis_client import (
    REDIS_UNAVAILABLE_ERRORS,
    AsyncRedisClient,
    RedisStreamEntry,
    RedisStreamRead,
    redis_text,
)

type RedisStreamTailItem = tuple[str, RedisStreamEntry]

_EMPTY_STREAM_ID = "0-0"
_INITIAL_BACKOFF_SECONDS = 0.5
_MAX_BACKOFF_SECONDS = 5.0


class RedisStreamTailClosedError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class RedisStreamTailStatus:
    healthy: bool
    sources: int
    subscribers: Mapping[str, int]
    overflows: Mapping[str, int]
    reader_failures: int
    last_failure: str | None


@dataclass(eq=False, slots=True)
class _Subscriber:
    label: str
    queue: asyncio.Queue[RedisStreamTailItem | None]
    overflowed: bool = False
    closed: bool = False

    def offer(self, item: RedisStreamTailItem) -> None:
        if self.overflowed:
            return
        try:
            self.queue.put_nowait(item)
        except asyncio.QueueFull:
            self.overflowed = True

    def terminate(self) -> None:
        self.closed = True
        while not self.queue.empty():
            self.queue.get_nowait()
        self.queue.put_nowait(None)


@dataclass(eq=False, slots=True)
class _Source:
    stream: str
    key: str
    cursor: str
    subscribers: set[_Subscriber] = field(default_factory=set)


@dataclass(slots=True)
class RedisStreamTailBroker:
    """One blocking multi-key XREAD per process, fanned out to bounded subscriber queues.

    A subscriber registers against a per-stream barrier under the same lock that
    advances the stream cursor during dispatch, so it receives exactly the entries
    after its barrier live and reads anything older straight from Redis.
    """

    redis: AsyncRedisClient
    block_milliseconds: int = 500
    page_count: int = 1_000
    queue_size: int = 1_024
    _sources: dict[str, _Source] = field(default_factory=dict, init=False, repr=False)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False, repr=False)
    _wake: asyncio.Event = field(default_factory=asyncio.Event, init=False, repr=False)
    _reader: asyncio.Task[None] | None = field(default=None, init=False, repr=False)
    _degraded: bool = field(default=False, init=False, repr=False)
    _reader_failures: int = field(default=0, init=False, repr=False)
    _last_failure: str | None = field(default=None, init=False, repr=False)
    _subscriber_counts: dict[str, int] = field(default_factory=dict, init=False, repr=False)
    _overflow_counts: dict[str, int] = field(default_factory=dict, init=False, repr=False)

    async def start(self) -> None:
        if self._reader is not None:
            raise RuntimeError("stream tail broker is already running")
        self._reader = asyncio.create_task(self._read_forever())

    async def close(self) -> None:
        reader = self._reader
        self._reader = None
        if reader is not None:
            reader.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await reader
        async with self._lock:
            subscribers = {
                subscriber for source in self._sources.values() for subscriber in source.subscribers
            }
            for subscriber in subscribers:
                subscriber.terminate()
            self._sources.clear()
            self._subscriber_counts.clear()

    def status(self) -> RedisStreamTailStatus:
        reader = self._reader
        return RedisStreamTailStatus(
            healthy=reader is not None and not reader.done() and not self._degraded,
            sources=len(self._sources),
            subscribers=dict(self._subscriber_counts),
            overflows=dict(self._overflow_counts),
            reader_failures=self._reader_failures,
            last_failure=self._last_failure,
        )

    async def subscribe(
        self,
        streams: Sequence[str],
        *,
        after: Mapping[str, str | None],
        label: str,
    ) -> RedisStreamTailSubscription:
        """Register for `streams`, replaying entries after `after[stream]` when it is set.

        A stream whose `after` is `None` delivers only entries appended after
        the subscription exists.
        """
        if self._reader is None or self._reader.done():
            raise RuntimeError("stream tail broker is not running")
        names = tuple(dict.fromkeys(stream for stream in streams if stream))
        if not names:
            raise ValueError("a stream tail subscription needs at least one stream")
        keys = {stream: self.redis.key(stream) for stream in names}
        # Tails are read outside the lock. An entry appended between this read
        # and registration sits after the barrier, so it arrives live rather
        # than by replay; nothing is skipped.
        tails = dict(
            zip(
                names,
                await asyncio.gather(*(self._tail(keys[stream]) for stream in names)),
                strict=True,
            )
        )
        subscriber = _Subscriber(label, asyncio.Queue(maxsize=self.queue_size))
        positions: dict[str, str] = {}
        barriers: dict[str, str] = {}
        async with self._lock:
            for stream in names:
                key = keys[stream]
                source = self._sources.get(key)
                if source is None:
                    source = _Source(stream=stream, key=key, cursor=tails[stream])
                    self._sources[key] = source
                    self._wake.set()
                source.subscribers.add(subscriber)
                barriers[stream] = source.cursor
                start = after.get(stream)
                positions[stream] = source.cursor if start is None else start
            self._subscriber_counts[label] = self._subscriber_counts.get(label, 0) + 1
        return RedisStreamTailSubscription(
            self,
            subscriber=subscriber,
            keys=keys,
            positions=positions,
            barriers=barriers,
        )

    async def _tail(self, key: str) -> str:
        entries = await self.redis.stream_reverse_range(key, count=1)
        if not entries:
            return _EMPTY_STREAM_ID
        return redis_text(entries[0][0])

    async def _resync(self, subscriber: _Subscriber, keys: Mapping[str, str]) -> dict[str, str]:
        async with self._lock:
            while not subscriber.queue.empty():
                subscriber.queue.get_nowait()
            subscriber.overflowed = False
            self._overflow_counts[subscriber.label] = (
                self._overflow_counts.get(subscriber.label, 0) + 1
            )
            return {
                stream: self._sources[key].cursor
                for stream, key in keys.items()
                if key in self._sources
            }

    async def _replay(
        self,
        stream: str,
        key: str,
        *,
        after: str,
        through: str,
    ) -> AsyncIterator[RedisStreamTailItem]:
        position = after
        limit = _entry_order(through)
        while _entry_order(position) < limit:
            pages = await self.redis.stream_read({key: position}, count=self.page_count)
            entries = pages[0][1] if pages else []
            if not entries:
                return
            for entry in entries:
                entry_id = redis_text(entry[0])
                if _entry_order(entry_id) > limit:
                    return
                position = entry_id
                yield (stream, entry)

    async def _detach(self, subscriber: _Subscriber, keys: Mapping[str, str]) -> None:
        async with self._lock:
            for key in keys.values():
                source = self._sources.get(key)
                if source is None:
                    continue
                source.subscribers.discard(subscriber)
                if not source.subscribers:
                    del self._sources[key]
            if not subscriber.closed:
                self._subscriber_counts[subscriber.label] -= 1

    async def _read_forever(self) -> None:
        try:
            backoff = _INITIAL_BACKOFF_SECONDS
            while True:
                async with self._lock:
                    offsets = {key: source.cursor for key, source in self._sources.items()}
                    self._wake.clear()
                if not offsets:
                    await self._wake.wait()
                    continue
                try:
                    pages = await self._read_pages(offsets)
                except REDIS_UNAVAILABLE_ERRORS as exc:
                    # Cursors are kept; the next successful read resumes from them.
                    self._degraded = True
                    self._reader_failures += 1
                    self._last_failure = type(exc).__name__
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, _MAX_BACKOFF_SECONDS)
                    continue
                backoff = _INITIAL_BACKOFF_SECONDS
                self._degraded = False
                async with self._lock:
                    for raw_key, entries in pages:
                        source = self._sources.get(redis_text(raw_key))
                        if source is None:
                            continue
                        cursor = _entry_order(source.cursor)
                        for entry in entries:
                            entry_id = redis_text(entry[0])
                            order = _entry_order(entry_id)
                            # A read issued before this source was recreated can
                            # carry entries its new barrier already covers.
                            if order <= cursor:
                                continue
                            cursor = order
                            source.cursor = entry_id
                            for subscriber in source.subscribers:
                                subscriber.offer((source.stream, entry))

        except asyncio.CancelledError:
            raise
        except BaseException as exc:
            # A reader that dies must not leave subscribers on heartbeats forever.
            self._last_failure = type(exc).__name__
            async with self._lock:
                subscribers = {
                    subscriber
                    for source in self._sources.values()
                    for subscriber in source.subscribers
                }
                for subscriber in subscribers:
                    subscriber.terminate()
                self._sources.clear()
            raise

    async def _read_pages(self, offsets: Mapping[str, str]) -> list[RedisStreamRead]:
        read = asyncio.create_task(
            self.redis.stream_read(offsets, count=self.page_count, block=self.block_milliseconds)
        )
        wake = asyncio.create_task(self._wake.wait())
        try:
            completed, _ = await asyncio.wait((read, wake), return_when=asyncio.FIRST_COMPLETED)
            return await read if read in completed else []
        finally:
            for task in (read, wake):
                if not task.done():
                    task.cancel()
            await asyncio.gather(read, wake, return_exceptions=True)


class RedisStreamTailSubscription:
    def __init__(
        self,
        broker: RedisStreamTailBroker,
        *,
        subscriber: _Subscriber,
        keys: Mapping[str, str],
        positions: dict[str, str],
        barriers: dict[str, str],
    ) -> None:
        self._broker = broker
        self._subscriber = subscriber
        self._keys = dict(keys)
        self._positions = positions
        self._barriers: dict[str, str] | None = barriers
        self._detached = False

    async def items(
        self,
        *,
        heartbeat_seconds: float,
    ) -> AsyncGenerator[RedisStreamTailItem | None]:
        """Entries in stream order; `None` after `heartbeat_seconds` without one.

        A queue that overflowed is discarded and the gap is read back from
        Redis, so a slow consumer costs Redis reads rather than lost entries.
        """
        subscriber = self._subscriber
        timeout = max(heartbeat_seconds, 0.05)
        while True:
            if self._barriers is None and subscriber.overflowed:
                self._barriers = await self._broker._resync(subscriber, self._keys)
            if self._barriers is not None:
                barriers = self._barriers
                self._barriers = None
                for stream, through in barriers.items():
                    replay = self._broker._replay(
                        stream,
                        self._keys[stream],
                        after=self._positions[stream],
                        through=through,
                    )
                    async for item in replay:
                        self._positions[stream] = redis_text(item[1][0])
                        yield item
            try:
                item = await asyncio.wait_for(subscriber.queue.get(), timeout=timeout)
            except TimeoutError:
                yield None
                continue
            if item is None:
                raise RedisStreamTailClosedError("stream tail broker closed")
            stream, entry = item
            self._positions[stream] = redis_text(entry[0])
            yield item

    async def close(self) -> None:
        if self._detached:
            return
        self._detached = True
        await self._broker._detach(self._subscriber, self._keys)

    async def __aenter__(self) -> RedisStreamTailSubscription:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.close()


def _entry_order(entry_id: str) -> tuple[int, int]:
    milliseconds, separator, sequence = entry_id.partition("-")
    if not separator:
        return (int(milliseconds), 0)
    return (int(milliseconds), int(sequence))


__all__ = [
    "RedisStreamTailBroker",
    "RedisStreamTailClosedError",
    "RedisStreamTailItem",
    "RedisStreamTailStatus",
    "RedisStreamTailSubscription",
]
