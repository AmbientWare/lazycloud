from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import suppress
from dataclasses import dataclass, field

from coordination.event_bus import EventBusEvent, EventBusEventType, event_channel_key, event_key
from coordination.redis_client import AsyncRedisClient, AsyncRedisSubscription, redis_text
from worker.events import WORKER_EVENT_HEARTBEAT_ID

_SUBSCRIBED_EVENT_TYPES = (
    EventBusEventType.StopContainer,
    EventBusEventType.StopBuild,
    EventBusEventType.PurgeSourceCache,
)


class WorkerEventBrokerUnavailable(RuntimeError):
    pass


def worker_event_target(event: EventBusEvent) -> str | None:
    """The worker an event addresses: an id, "" for every worker, None for none.

    A stop-container event without a worker id addresses nobody rather than
    everybody; delivering it to every worker would stop the container wherever
    it happened to land.
    """
    target = event.args.get("worker_id")
    worker_id = target.strip() if isinstance(target, str) else ""
    if event.type == EventBusEventType.StopContainer and not worker_id:
        return None
    return worker_id


@dataclass(eq=False, slots=True)
class _WorkerEventSubscriber:
    queue: asyncio.Queue[str | None]
    overflowed: bool = False


@dataclass(slots=True)
class WorkerEventBroker:
    """One Redis pub/sub subscription per process, fanned out per worker.

    Pub/sub carries only event ids; the event itself is read back from Redis
    so a subscriber never sees an id it cannot resolve. A subscriber whose
    queue overflows has its stream ended rather than resynced: the pending
    set the worker reads on reconnect is the durable record, not the channel.
    """

    redis: AsyncRedisClient
    queue_size: int = 256
    _subscription: AsyncRedisSubscription | None = field(default=None, init=False, repr=False)
    _reader: asyncio.Task[None] | None = field(default=None, init=False, repr=False)
    _reader_failure: BaseException | None = field(default=None, init=False, repr=False)
    _subscribers: dict[str, set[_WorkerEventSubscriber]] = field(
        default_factory=dict,
        init=False,
        repr=False,
    )
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False, repr=False)

    async def start(self) -> None:
        if self._reader is not None:
            raise RuntimeError("worker event broker is already running")
        subscription = self.redis.pubsub(ignore_subscribe_messages=True)
        try:
            await subscription.subscribe(
                *(
                    self.redis.key(event_channel_key(event_type))
                    for event_type in _SUBSCRIBED_EVENT_TYPES
                )
            )
        except BaseException:
            await subscription.close()
            raise
        self._reader_failure = None
        self._subscription = subscription
        self._reader = asyncio.create_task(self._read_messages(subscription))

    async def close(self) -> None:
        reader = self._reader
        self._reader = None
        if reader is not None:
            reader.cancel()
            with suppress(asyncio.CancelledError):
                await reader
        subscription = self._subscription
        self._subscription = None
        if subscription is not None:
            await subscription.close()
        async with self._lock:
            self._subscribers.clear()

    async def stream_event_ids(
        self,
        worker_id: str,
        *,
        heartbeat_interval_seconds: float,
        max_events: int,
    ) -> AsyncIterator[str]:
        if self._reader is None:
            raise RuntimeError("worker event broker is not running")
        subscriber = _WorkerEventSubscriber(asyncio.Queue(maxsize=self.queue_size))
        async with self._lock:
            if self._reader_failure is not None:
                raise WorkerEventBrokerUnavailable("worker event broker reader failed") from (
                    self._reader_failure
                )
            self._subscribers.setdefault(worker_id, set()).add(subscriber)
        emitted = 0
        heartbeat_seconds = max(heartbeat_interval_seconds, 0.1)
        try:
            while max_events <= 0 or emitted < max_events:
                if subscriber.overflowed:
                    return
                try:
                    event_id = await asyncio.wait_for(
                        subscriber.queue.get(),
                        timeout=heartbeat_seconds,
                    )
                except TimeoutError:
                    event_id = WORKER_EVENT_HEARTBEAT_ID
                if event_id is None:
                    raise WorkerEventBrokerUnavailable("worker event broker reader failed") from (
                        self._reader_failure
                    )
                yield event_id
                emitted += 1
        finally:
            async with self._lock:
                subscribers = self._subscribers.get(worker_id)
                if subscribers is not None:
                    subscribers.discard(subscriber)
                    if not subscribers:
                        del self._subscribers[worker_id]

    async def event(self, event_id: str) -> EventBusEvent | None:
        raw = await self.redis.get(self.redis.key(event_key(event_id)))
        if raw is None:
            return None
        try:
            return EventBusEvent.model_validate_json(redis_text(raw))
        except ValueError:
            return None

    async def _read_messages(self, subscription: AsyncRedisSubscription) -> None:
        try:
            while True:
                message = await subscription.get_message(timeout=1.0)
                if message is None or redis_text(message.type) != "message":
                    continue
                event_id = redis_text(message.data)
                event = await self.event(event_id)
                if event is not None:
                    await self._dispatch(event_id, event)
        except asyncio.CancelledError:
            raise
        except BaseException as exc:
            self._reader_failure = exc
            await self._terminate_subscribers()
            raise

    async def _terminate_subscribers(self) -> None:
        async with self._lock:
            for worker_subscribers in self._subscribers.values():
                for subscriber in worker_subscribers:
                    while not subscriber.queue.empty():
                        subscriber.queue.get_nowait()
                    subscriber.queue.put_nowait(None)

    async def _dispatch(self, event_id: str, event: EventBusEvent) -> None:
        target = worker_event_target(event)
        if target is None:
            return
        async with self._lock:
            if target:
                subscribers = tuple(self._subscribers.get(target, ()))
            else:
                subscribers = tuple(
                    subscriber
                    for worker_subscribers in self._subscribers.values()
                    for subscriber in worker_subscribers
                )
        for subscriber in subscribers:
            try:
                subscriber.queue.put_nowait(event_id)
            except asyncio.QueueFull:
                subscriber.overflowed = True


__all__ = ["WorkerEventBroker", "WorkerEventBrokerUnavailable", "worker_event_target"]
