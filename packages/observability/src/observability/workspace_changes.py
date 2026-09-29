from __future__ import annotations

import asyncio
import logging
import re
import threading
import time
from collections.abc import AsyncGenerator, AsyncIterator, Iterator
from concurrent.futures import Future
from contextlib import aclosing, asynccontextmanager, contextmanager, suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Protocol
from uuid import uuid4

from coordination.redis_client import AsyncRedisClient, RedisClient, RedisStreamEntry, redis_text
from coordination.stream_tail import RedisStreamTailBroker, RedisStreamTailSubscription
from pydantic import ValidationError
from redis.exceptions import RedisError
from shared.errors import InvalidInputError
from shared.http.workspace_changes import (
    WorkspaceChangeEvent,
    WorkspaceChangeTopic,
    WorkspaceChangeType,
)
from shared.workload_keys import workload_readiness_stream_name

logger = logging.getLogger(__name__)

DEFAULT_WORKSPACE_CHANGE_STREAM_MAX_LENGTH = 10_000
WORKSPACE_CHANGE_SSE_EVENT = "workspace.change"

_STREAM_ID_PATTERN = re.compile(r"^[0-9]+-[0-9]+$")


@dataclass(frozen=True, slots=True)
class WorkspaceChangeRecord:
    entry_id: str
    event: WorkspaceChangeEvent


class WorkspaceChangePublisher(Protocol):
    def emit_change(
        self,
        *,
        workspace_id: str,
        topic: WorkspaceChangeTopic,
        change: WorkspaceChangeType,
        resource_id: str,
        app_id: str | None = None,
        deployment_id: str | None = None,
        stub_id: str | None = None,
        task_id: str | None = None,
        root_task_id: str | None = None,
        container_id: str | None = None,
        occurred_at: datetime | None = None,
        event_id: str | None = None,
    ) -> WorkspaceChangeEvent | None: ...


@dataclass(slots=True)
class WorkspaceChangeRepository:
    redis: RedisClient
    max_length: int = DEFAULT_WORKSPACE_CHANGE_STREAM_MAX_LENGTH

    def __post_init__(self) -> None:
        if self.max_length <= 0:
            raise ValueError("workspace change stream max length must be positive")

    def append(self, event: WorkspaceChangeEvent) -> str:
        entry_id = self.redis.stream_add(
            self.stream_key(event.workspace_id),
            {"event": event.model_dump_json()},
            id="*",
            maxlen=self.max_length,
            approximate=True,
        )
        return redis_text(entry_id)

    def delete_workspace(self, workspace_id: str) -> int:
        return int(self.redis.delete(self.stream_key(workspace_id)))

    def stream_key(self, workspace_id: str) -> str:
        return _workspace_change_stream_key(self.redis, workspace_id)


@dataclass(slots=True)
class AsyncWorkspaceChangeReader:
    tail: RedisStreamTailBroker

    async def follow(
        self,
        workspace_id: str,
        *,
        after: str | None,
        max_events: int,
        heartbeat_seconds: float,
    ) -> AsyncIterator[WorkspaceChangeRecord | None]:
        """Changes after `after`, or only new ones when it is `None`.

        A `None` item is a heartbeat.
        """
        stream = workspace_change_stream_name(workspace_id)
        cursor = validate_workspace_change_cursor(after) if after is not None else None
        subscription = await self.tail.subscribe(
            (stream,),
            after={stream: cursor},
            label="workspace-changes",
        )
        return _followed_changes(
            subscription,
            max_events=max_events,
            heartbeat_seconds=heartbeat_seconds,
        )


@dataclass(slots=True)
class AsyncWorkloadChangeReader:
    tail: RedisStreamTailBroker

    @asynccontextmanager
    async def follow(
        self, *, workspace_id: str, stub_id: str
    ) -> AsyncIterator[AsyncIterator[None]]:
        stream = workspace_change_stream_name(workspace_id)
        readiness = workload_readiness_stream_name(stub_id)
        subscription = await self.tail.subscribe(
            (stream, readiness), after={stream: None, readiness: None}, label="workload-readiness"
        )
        changed = asyncio.Event()
        async with subscription:
            listener = asyncio.create_task(self._listen(subscription, stub_id, changed))
            try:
                async with aclosing(self._updates(changed, listener)) as updates:
                    yield updates
            finally:
                listener.cancel()
                with suppress(asyncio.CancelledError):
                    await listener

    async def _listen(
        self,
        subscription: RedisStreamTailSubscription,
        stub_id: str,
        changed: asyncio.Event,
    ) -> None:
        try:
            async with aclosing(subscription.items(heartbeat_seconds=0.5)) as items:
                async for item in items:
                    if item is None:
                        continue
                    stream, entry = item
                    if stream == workload_readiness_stream_name(stub_id):
                        changed.set()
                        continue
                    event = workspace_change_record(entry).event
                    if event.stub_id == stub_id and event.topic in {
                        WorkspaceChangeTopic.Containers,
                        WorkspaceChangeTopic.Tasks,
                        WorkspaceChangeTopic.Workloads,
                    }:
                        changed.set()
        finally:
            changed.set()

    async def _updates(
        self, changed: asyncio.Event, listener: asyncio.Task[None]
    ) -> AsyncGenerator[None]:
        # Recover missed lifecycle events and probe workloads that have not bound a port yet.
        recovery_seconds = 0.5
        next_discovery = 0.0
        while True:
            try:
                async with asyncio.timeout(recovery_seconds):
                    await changed.wait()
            except TimeoutError:
                pass
            if listener.done():
                await listener
                raise RuntimeError("workload notification stream closed")
            # Collapse buffered events and bound SQL discovery during completion bursts.
            await asyncio.sleep(max(next_discovery - time.monotonic(), 0.0))
            changed.clear()
            next_discovery = time.monotonic() + 0.05
            yield None


@dataclass(slots=True)
class WorkloadChangeSubscription:
    changed: threading.Event = field(default_factory=threading.Event)
    completion: Future[None] = field(default_factory=Future)

    def wait(self, timeout_seconds: float) -> bool:
        signalled = timeout_seconds > 0 and self.changed.wait(timeout_seconds)
        self.changed.clear()
        if self.completion.done():
            self.completion.result()
            raise RuntimeError("workload notification subscription closed")
        return signalled


@dataclass(slots=True)
class SyncWorkloadChangeReader:
    reader: AsyncWorkloadChangeReader
    _loop: asyncio.AbstractEventLoop | None = field(default=None, init=False)
    _tasks: set[asyncio.Task[None]] = field(default_factory=set, init=False)

    async def start(self) -> None:
        if self._loop is not None:
            raise RuntimeError("synchronous workload reader is already running")
        self._loop = asyncio.get_running_loop()

    async def close(self) -> None:
        if self._loop is not None and self._loop is not asyncio.get_running_loop():
            raise RuntimeError("workload reader must close on its owning event loop")
        self._loop = None
        tasks = tuple(self._tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    @contextmanager
    def follow(self, *, workspace_id: str, stub_id: str) -> Iterator[WorkloadChangeSubscription]:
        loop = self._loop
        if loop is None or not loop.is_running():
            raise RuntimeError("synchronous workload reader is not running")
        try:
            current_loop = asyncio.get_running_loop()
        except RuntimeError:
            current_loop = None
        if current_loop is not None:
            raise RuntimeError("synchronous workload discovery cannot block an event loop")
        subscription = WorkloadChangeSubscription()
        started = asyncio.run_coroutine_threadsafe(
            self._subscribe(subscription, workspace_id=workspace_id, stub_id=stub_id), loop
        )
        try:
            task = started.result()
        except BaseException:
            started.cancel()
            raise
        try:
            yield subscription
        finally:
            if not task.done():
                if not loop.is_running():
                    raise RuntimeError("workload reader event loop stopped before unsubscribe")
                asyncio.run_coroutine_threadsafe(self._unsubscribe(task), loop).result()

    async def _subscribe(
        self, subscription: WorkloadChangeSubscription, *, workspace_id: str, stub_id: str
    ) -> asyncio.Task[None]:
        if self._loop is None:
            raise RuntimeError("synchronous workload reader is not running")
        ready = asyncio.get_running_loop().create_future()
        task = asyncio.create_task(
            self._relay(subscription, ready, workspace_id=workspace_id, stub_id=stub_id)
        )
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        try:
            await ready
        except BaseException:
            await self._unsubscribe(task)
            raise
        return task

    async def _relay(
        self,
        subscription: WorkloadChangeSubscription,
        ready: asyncio.Future[None],
        *,
        workspace_id: str,
        stub_id: str,
    ) -> None:
        try:
            async with self.reader.follow(workspace_id=workspace_id, stub_id=stub_id) as changes:
                ready.set_result(None)
                async for _ in changes:
                    subscription.changed.set()
            raise RuntimeError("workload notification stream closed")
        except BaseException as exc:
            failure = (
                RuntimeError("workload notification subscription closed")
                if isinstance(exc, asyncio.CancelledError)
                else exc
            )
            if not ready.done():
                ready.set_exception(failure)
            subscription.completion.set_exception(failure)
        finally:
            subscription.changed.set()

    @staticmethod
    async def _unsubscribe(task: asyncio.Task[None]) -> None:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@dataclass(slots=True)
class AsyncWorkspaceChangeService:
    redis: AsyncRedisClient
    max_length: int = DEFAULT_WORKSPACE_CHANGE_STREAM_MAX_LENGTH

    def __post_init__(self) -> None:
        if self.max_length <= 0:
            raise ValueError("workspace change stream max length must be positive")

    async def emit_change(
        self,
        *,
        workspace_id: str,
        topic: WorkspaceChangeTopic,
        change: WorkspaceChangeType,
        resource_id: str,
        app_id: str | None = None,
        deployment_id: str | None = None,
        stub_id: str | None = None,
        task_id: str | None = None,
        root_task_id: str | None = None,
        container_id: str | None = None,
        occurred_at: datetime | None = None,
        event_id: str | None = None,
    ) -> WorkspaceChangeEvent | None:
        event = _workspace_change_event(
            workspace_id=workspace_id,
            topic=topic,
            change=change,
            resource_id=resource_id,
            app_id=app_id,
            deployment_id=deployment_id,
            stub_id=stub_id,
            task_id=task_id,
            root_task_id=root_task_id,
            container_id=container_id,
            occurred_at=occurred_at,
            event_id=event_id,
        )
        try:
            await self.redis.stream_add(
                _workspace_change_stream_key(self.redis, workspace_id),
                {"event": event.model_dump_json()},
                id="*",
                maxlen=self.max_length,
                approximate=True,
            )
        except (RedisError, OSError):
            _log_publication_failure(event)
            return None
        return event


@dataclass(slots=True)
class WorkspaceChangeService:
    repository: WorkspaceChangeRepository

    def publish(self, event: WorkspaceChangeEvent) -> WorkspaceChangeEvent | None:
        try:
            self.repository.append(event)
        except (RedisError, OSError):
            _log_publication_failure(event)
            return None
        return event

    def emit_change(
        self,
        *,
        workspace_id: str,
        topic: WorkspaceChangeTopic,
        change: WorkspaceChangeType,
        resource_id: str,
        app_id: str | None = None,
        deployment_id: str | None = None,
        stub_id: str | None = None,
        task_id: str | None = None,
        root_task_id: str | None = None,
        container_id: str | None = None,
        occurred_at: datetime | None = None,
        event_id: str | None = None,
    ) -> WorkspaceChangeEvent | None:
        return self.publish(
            _workspace_change_event(
                workspace_id=workspace_id,
                topic=topic,
                change=change,
                resource_id=resource_id,
                app_id=app_id,
                deployment_id=deployment_id,
                stub_id=stub_id,
                task_id=task_id,
                root_task_id=root_task_id,
                container_id=container_id,
                occurred_at=occurred_at,
                event_id=event_id,
            )
        )

    def delete_workspace(self, workspace_id: str) -> int:
        try:
            return self.repository.delete_workspace(workspace_id)
        except (RedisError, OSError):
            logger.exception(
                "workspace change cleanup failed: workspace_id=%s",
                workspace_id,
            )
            return 0


def validate_workspace_change_cursor(value: str) -> str:
    normalized = value.strip()
    if not _STREAM_ID_PATTERN.fullmatch(normalized):
        raise InvalidInputError("Last-Event-ID must be a Redis stream entry id")
    return normalized


def workspace_change_stream_name(workspace_id: str) -> str:
    normalized = workspace_id.strip()
    if not normalized:
        raise ValueError("workspace id must not be empty")
    return f"workspace-changes:{normalized}"


def _workspace_change_stream_key(
    redis: RedisClient | AsyncRedisClient,
    workspace_id: str,
) -> str:
    return redis.key(workspace_change_stream_name(workspace_id))


def _workspace_change_event(
    *,
    workspace_id: str,
    topic: WorkspaceChangeTopic,
    change: WorkspaceChangeType,
    resource_id: str,
    app_id: str | None,
    deployment_id: str | None,
    stub_id: str | None,
    task_id: str | None,
    root_task_id: str | None,
    container_id: str | None,
    occurred_at: datetime | None,
    event_id: str | None,
) -> WorkspaceChangeEvent:
    return WorkspaceChangeEvent(
        event_id=event_id or str(uuid4()),
        occurred_at=occurred_at or datetime.now(UTC),
        workspace_id=workspace_id,
        topic=topic,
        change=change,
        resource_id=resource_id,
        app_id=app_id,
        deployment_id=deployment_id,
        stub_id=stub_id,
        task_id=task_id,
        root_task_id=root_task_id,
        container_id=container_id,
    )


def _log_publication_failure(event: WorkspaceChangeEvent) -> None:
    logger.exception(
        "workspace change publication failed: workspace_id=%s topic=%s resource_id=%s",
        event.workspace_id,
        event.topic.value,
        event.resource_id,
    )


async def _followed_changes(
    subscription: RedisStreamTailSubscription,
    *,
    max_events: int,
    heartbeat_seconds: float,
) -> AsyncIterator[WorkspaceChangeRecord | None]:
    emitted = 0
    try:
        async for item in subscription.items(heartbeat_seconds=heartbeat_seconds):
            if item is None:
                yield None
                continue
            yield workspace_change_record(item[1])
            emitted += 1
            if max_events > 0 and emitted >= max_events:
                return
    finally:
        await subscription.close()


def workspace_change_record(entry: RedisStreamEntry) -> WorkspaceChangeRecord:
    raw_entry_id, raw_fields = entry
    entry_id = validate_workspace_change_cursor(redis_text(raw_entry_id))
    fields = {redis_text(key): value for key, value in raw_fields.items()}
    payload = fields.get("event")
    if payload is None:
        raise RuntimeError("Redis workspace change entry is missing its event")
    try:
        event = WorkspaceChangeEvent.model_validate_json(redis_text(payload))
    except ValidationError as exc:
        raise RuntimeError("Redis workspace change entry has an invalid event") from exc
    return WorkspaceChangeRecord(entry_id=entry_id, event=event)


__all__ = [
    "DEFAULT_WORKSPACE_CHANGE_STREAM_MAX_LENGTH",
    "WORKSPACE_CHANGE_SSE_EVENT",
    "AsyncWorkspaceChangeReader",
    "AsyncWorkspaceChangeService",
    "WorkspaceChangePublisher",
    "WorkspaceChangeRecord",
    "WorkspaceChangeRepository",
    "WorkspaceChangeService",
    "validate_workspace_change_cursor",
    "workspace_change_record",
    "workspace_change_stream_name",
]
