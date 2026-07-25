from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol
from uuid import uuid4

from coordination.redis_client import RedisClient, redis_text
from pydantic import TypeAdapter, ValidationError
from redis.exceptions import RedisError
from shared.errors import InvalidInputError
from shared.http.workspace_changes import (
    WorkspaceChangeEvent,
    WorkspaceChangeTopic,
    WorkspaceChangeType,
)

logger = logging.getLogger(__name__)

DEFAULT_WORKSPACE_CHANGE_STREAM_MAX_LENGTH = 10_000
DEFAULT_WORKSPACE_CHANGE_STREAM_READ_COUNT = 100
DEFAULT_WORKSPACE_CHANGE_STREAM_BLOCK_MILLISECONDS = 1_000
WORKSPACE_CHANGE_STREAM_HEARTBEAT_SECONDS = 15.0
WORKSPACE_CHANGE_SSE_EVENT = "workspace.change"

_STREAM_ID_PATTERN = re.compile(r"^[0-9]+-[0-9]+$")

type RedisWireScalar = str | bytes | int | float
type RedisStreamFields = dict[RedisWireScalar, RedisWireScalar]
type RedisStreamEntry = tuple[RedisWireScalar, RedisStreamFields]
type RedisStreamPage = tuple[RedisWireScalar, list[RedisStreamEntry]]

_REDIS_STREAM_ENTRIES_ADAPTER = TypeAdapter(list[RedisStreamEntry])
_REDIS_STREAM_PAGES_ADAPTER = TypeAdapter(list[RedisStreamPage])


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

    def current_entry_id(self, workspace_id: str) -> str:
        entries = _redis_stream_entries(
            self.redis.stream_reverse_range(self.stream_key(workspace_id), count=1)
        )
        if not entries:
            return "0-0"
        entry_id, _fields = entries[0]
        return redis_text(entry_id)

    def read_after(
        self,
        workspace_id: str,
        entry_id: str,
        *,
        block_milliseconds: int = DEFAULT_WORKSPACE_CHANGE_STREAM_BLOCK_MILLISECONDS,
        count: int = DEFAULT_WORKSPACE_CHANGE_STREAM_READ_COUNT,
    ) -> tuple[WorkspaceChangeRecord, ...]:
        cursor = validate_workspace_change_cursor(entry_id)
        response = _redis_stream_pages(
            self.redis.stream_read(
                {self.stream_key(workspace_id): cursor},
                count=max(count, 1),
                block=max(block_milliseconds, 0) or None,
            )
        )
        records: list[WorkspaceChangeRecord] = []
        for _stream, entries in response:
            for entry in entries:
                records.append(_workspace_change_record(entry))
        return tuple(records)

    def delete_workspace(self, workspace_id: str) -> int:
        return int(self.redis.delete(self.stream_key(workspace_id)))

    def stream_key(self, workspace_id: str) -> str:
        normalized = workspace_id.strip()
        if not normalized:
            raise ValueError("workspace id must not be empty")
        return self.redis.key("workspace-changes", normalized)


@dataclass(slots=True)
class WorkspaceChangeService:
    repository: WorkspaceChangeRepository

    def publish(self, event: WorkspaceChangeEvent) -> WorkspaceChangeEvent | None:
        try:
            self.repository.append(event)
        except (RedisError, OSError):
            logger.exception(
                "workspace change publication failed: workspace_id=%s topic=%s resource_id=%s",
                event.workspace_id,
                event.topic.value,
                event.resource_id,
            )
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
            WorkspaceChangeEvent(
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


def _redis_stream_entries(value: list[RedisStreamEntry]) -> list[RedisStreamEntry]:
    try:
        return _REDIS_STREAM_ENTRIES_ADAPTER.validate_python(value)
    except ValidationError as exc:
        raise RuntimeError("Redis workspace change entries have an invalid shape") from exc


def _redis_stream_pages(value: list[RedisStreamPage]) -> list[RedisStreamPage]:
    try:
        return _REDIS_STREAM_PAGES_ADAPTER.validate_python(value)
    except ValidationError as exc:
        raise RuntimeError("Redis workspace change response has an invalid shape") from exc


__all__ = [
    "DEFAULT_WORKSPACE_CHANGE_STREAM_BLOCK_MILLISECONDS",
    "DEFAULT_WORKSPACE_CHANGE_STREAM_MAX_LENGTH",
    "DEFAULT_WORKSPACE_CHANGE_STREAM_READ_COUNT",
    "WORKSPACE_CHANGE_SSE_EVENT",
    "WORKSPACE_CHANGE_STREAM_HEARTBEAT_SECONDS",
    "WorkspaceChangePublisher",
    "WorkspaceChangeRecord",
    "WorkspaceChangeRepository",
    "WorkspaceChangeService",
    "validate_workspace_change_cursor",
]
