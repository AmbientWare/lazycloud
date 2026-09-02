from __future__ import annotations

import logging
import re
from collections.abc import AsyncIterator
from dataclasses import dataclass
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
            yield _workspace_change_record(item[1])
            emitted += 1
            if max_events > 0 and emitted >= max_events:
                return
    finally:
        await subscription.close()


def _workspace_change_record(entry: RedisStreamEntry) -> WorkspaceChangeRecord:
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
    "workspace_change_stream_name",
]
