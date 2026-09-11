from __future__ import annotations

import hashlib
import json
import time
from collections.abc import AsyncGenerator, AsyncIterator, Callable, Iterable, Mapping
from contextlib import aclosing, asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from uuid import uuid4

from coordination.redis_client import (
    AsyncRedisClient,
    RedisClient,
    RedisStreamEntry,
    RedisWireScalar,
    redis_text,
)
from coordination.stream_tail import RedisStreamTailBroker, RedisStreamTailSubscription
from pydantic import JsonValue, TypeAdapter, ValidationError
from shared.errors import ExpiredCursorError, InvalidInputError
from shared.http.observability import LogRecord
from shared.http.workspace_changes import WorkspaceChangeTopic
from shared.logs import ContainerLogEntryKind
from shared.realtime.contracts import (
    CloudEventRecord,
    EventDataInput,
    EventRecordType,
    create_cloud_event_record,
    event_metadata_from_cloud_event,
)
from shared.realtime.streams import (
    EventHistoryQuery,
    EventSequencedRecord,
    EventStreamPlanner,
    LogStreamQuery,
    event_record_headers_skip,
    event_stream_part,
    log_record_headers_skip,
)
from shared.serialization import to_json_value

from observability.workspace_changes import workspace_change_record, workspace_change_stream_name

DEFAULT_REDIS_EVENT_STREAM_READ_LIMIT = 10_000
REALTIME_STREAM_TTL_SECONDS = 7 * 24 * 60 * 60
REALTIME_STREAM_MAX_ENTRIES = 50_000


@dataclass(slots=True)
class AsyncTaskChangeReader:
    tail: RedisStreamTailBroker
    planner: EventStreamPlanner = field(default_factory=EventStreamPlanner)

    @asynccontextmanager
    async def follow(
        self,
        *,
        workspace_id: str,
        stub_id: str,
        task_id: str,
        reconcile_seconds: float = 1.0,
    ) -> AsyncIterator[AsyncIterator[None]]:
        changes = workspace_change_stream_name(workspace_id)
        logs = (
            self.planner.stub_task_stream_name(workspace_id, stub_id)
            if stub_id
            else self.planner.workspace_log_stream_name(workspace_id)
        )
        subscription = await self.tail.subscribe(
            (changes, logs), after={changes: None, logs: None}, label="function-invocations"
        )
        async with (
            subscription,
            aclosing(self._updates(subscription, changes, task_id, reconcile_seconds)) as updates,
        ):
            yield updates

    async def _updates(
        self,
        subscription: RedisStreamTailSubscription,
        changes: str,
        task_id: str,
        reconcile_seconds: float,
    ) -> AsyncGenerator[None]:
        deadline = time.monotonic() + reconcile_seconds
        async with aclosing(subscription.items(heartbeat_seconds=reconcile_seconds)) as items:
            async for item in items:
                relevant = item is None
                if item is not None:
                    stream, entry = item
                    if stream == changes:
                        event = workspace_change_record(entry).event
                        relevant = (
                            event.topic is WorkspaceChangeTopic.Tasks
                            and event.resource_id == task_id
                        )
                    else:
                        record = _record_from_entry(stream, entry)
                        relevant = record is not None and record.headers.get("task_id") == task_id
                if relevant or time.monotonic() >= deadline:
                    deadline = time.monotonic() + reconcile_seconds
                    yield None


_CONTAINER_LOG_SCRIPT_RESULT_ADAPTER = TypeAdapter(tuple[int, int, int])
_JSON_OBJECT_ADAPTER = TypeAdapter(dict[str, JsonValue])

_APPEND_EVENT_SCRIPT = """
local max_entries = tonumber(ARGV[1])
local stream_ttl = tonumber(ARGV[2])

for stream_index = 1, #KEYS do
    redis.call(
        'XADD',
        KEYS[stream_index],
        'MAXLEN',
        '~',
        max_entries,
        '*',
        'body',
        ARGV[3],
        'headers',
        ARGV[4]
    )
    redis.call('EXPIRE', KEYS[stream_index], stream_ttl)
end

return #KEYS
"""

_APPEND_CONTAINER_LOG_BATCH_SCRIPT = """
local first_sequence = tonumber(ARGV[1])
local entry_count = tonumber(ARGV[2])
local cursor_ttl = tonumber(ARGV[3])
local stream_ttl = tonumber(ARGV[4])
local max_entries = tonumber(ARGV[5])
local next_sequence = tonumber(redis.call('GET', KEYS[1]) or '0')

if first_sequence > next_sequence then
    return {-1, next_sequence, 0}
end

local batch_end = first_sequence + entry_count
if batch_end <= next_sequence then
    redis.call('EXPIRE', KEYS[1], cursor_ttl)
    return {0, next_sequence, 0}
end

local first_unwritten = next_sequence - first_sequence
local appended = 0
for entry_index = first_unwritten, entry_count - 1 do
    local body_index = 6 + (entry_index * 2)
    for stream_index = 2, #KEYS do
        redis.call(
            'XADD',
            KEYS[stream_index],
            'MAXLEN',
            '~',
            max_entries,
            '*',
            'body',
            ARGV[body_index],
            'headers',
            ARGV[body_index + 1]
        )
    end
    appended = appended + 1
end

for stream_index = 2, #KEYS do
    redis.call('EXPIRE', KEYS[stream_index], stream_ttl)
end
redis.call('SET', KEYS[1], batch_end, 'EX', cursor_ttl)
return {0, batch_end, appended}
"""

_FIRST_STREAM_ENTRY_ID_SCRIPT = """
local entries = redis.call('XRANGE', KEYS[1], '-', '+', 'COUNT', 1)
if #entries == 0 then
    return false
end
return entries[1][1]
"""


@dataclass(frozen=True, slots=True)
class RealtimeStreamRetention:
    ttl_seconds: int = REALTIME_STREAM_TTL_SECONDS
    max_entries: int = REALTIME_STREAM_MAX_ENTRIES

    def __post_init__(self) -> None:
        if self.ttl_seconds <= 0:
            raise ValueError("realtime stream TTL must be positive")
        if self.max_entries <= 0:
            raise ValueError("realtime stream maximum entries must be positive")


@dataclass(frozen=True, slots=True)
class RedisStreamRecord:
    stream: str
    entry_id: str
    headers: Mapping[str, JsonValue]
    body: Mapping[str, JsonValue]

    @property
    def event_name(self) -> str:
        event_type = self.headers.get("type") or self.body.get("type")
        return str(event_type) if event_type else "event"


@dataclass(frozen=True, slots=True)
class RedisContainerLogBatchAppendResult:
    accepted_through: int
    appended_count: int
    sequence_gap: bool = False


@dataclass(slots=True)
class RedisEventStreamRepository:
    redis: RedisClient
    planner: EventStreamPlanner = field(default_factory=EventStreamPlanner)
    retention: RealtimeStreamRetention = field(default_factory=RealtimeStreamRetention)

    def append_container_log_batch(
        self,
        *,
        container_id: str,
        capture_id: str,
        first_sequence: int,
        events: tuple[CloudEventRecord, ...],
    ) -> RedisContainerLogBatchAppendResult:
        if first_sequence < 0:
            raise ValueError("container log first sequence must not be negative")
        if not events:
            raise ValueError("container log batch must not be empty")

        plans = tuple(self.planner.append_record_for_event(event) for event in events)
        streams = plans[0].streams
        if not streams:
            raise ValueError("container log batch has no Redis fanout streams")
        if any(plan.streams != streams for plan in plans[1:]):
            raise ValueError("container log batch metadata changed within one capture request")

        workspace_ids = {event_metadata_from_cloud_event(event).workspace_id for event in events}
        if len(workspace_ids) != 1 or not next(iter(workspace_ids)):
            raise ValueError("container log batch must belong to one workspace")
        workspace_id = next(iter(workspace_ids))

        cursor_digest = hashlib.sha256(f"{container_id}\0{capture_id}".encode()).hexdigest()
        keys = [
            self._ingest_cursor_key(workspace_id, cursor_digest),
            *(self._stream_key(stream) for stream in streams),
        ]
        arguments: list[str | int] = [
            first_sequence,
            len(events),
            self.retention.ttl_seconds,
            self.retention.ttl_seconds,
            self.retention.max_entries,
        ]
        for plan in plans:
            arguments.extend((_json(plan.body), _json(plan.headers)))

        raw_result = self.redis.eval_scalars(
            _APPEND_CONTAINER_LOG_BATCH_SCRIPT,
            len(keys),
            *keys,
            *arguments,
        )
        status_code, next_sequence, appended_count = _container_log_script_result(raw_result)
        return RedisContainerLogBatchAppendResult(
            accepted_through=next_sequence - 1,
            appended_count=appended_count,
            sequence_gap=status_code < 0,
        )

    def append_event(
        self,
        event_type: str | EventRecordType,
        data: EventDataInput,
        *,
        event_id: str | None = None,
    ) -> CloudEventRecord:
        event, streams, body, headers = _event_append_data(
            self.planner,
            event_type,
            data,
            event_id=event_id,
        )
        stream_keys = tuple(self._stream_key(stream) for stream in streams)
        if stream_keys:
            self.redis.eval_int(
                _APPEND_EVENT_SCRIPT,
                len(stream_keys),
                *stream_keys,
                self.retention.max_entries,
                self.retention.ttl_seconds,
                body,
                headers,
            )
        return event

    def delete_workspace(self, workspace_id: str) -> int:
        stream_roots = (
            self.planner.workspace_stream_name(workspace_id),
            self.planner.workspace_log_stream_name(workspace_id),
            self._ingest_cursor_root(workspace_id),
        )
        keys: list[str] = []
        for stream_root in stream_roots:
            root_key = self._stream_key(stream_root)
            keys.append(root_key)
            keys.extend(self.redis.scan(f"{root_key}/*"))
        unique_keys = tuple(dict.fromkeys(keys))
        return int(self.redis.delete(*unique_keys))

    def read_event_history(
        self,
        query: EventHistoryQuery,
        *,
        limit: int | None = None,
        cursor: str | None = None,
        clamp: bool | None = None,
    ) -> tuple[RedisStreamRecord, ...]:
        plan = self.planner.plan_event_history_read(query)
        if cursor is not None:
            self._raise_if_cursor_expired(
                (*plan.initial_streams, *plan.fallback_streams),
                cursor=cursor,
                clamp=clamp,
            )
        normalized_cursor = _normalize_entry_id(cursor) if cursor is not None else None
        read_limit = limit or plan.read_limit
        records = _filter_event_records(
            self._read_stream_records(plan.initial_streams, limit=read_limit),
            query=plan.query,
            cursor=normalized_cursor,
            limit=read_limit,
        )
        if records or not plan.fallback_streams:
            return records
        return _filter_event_records(
            self._read_stream_records(plan.fallback_streams, limit=read_limit),
            query=plan.query,
            cursor=normalized_cursor,
            limit=read_limit,
        )

    def read_logs(
        self,
        query: LogStreamQuery,
        *,
        limit: int | None = None,
    ) -> tuple[RedisStreamRecord, ...]:
        plan = self.planner.plan_log_page(query)
        self._raise_if_log_cursor_expired(plan.streams, plan.query)
        read_limit = limit or plan.limit
        records = self._read_stream_records(
            plan.streams,
            limit=plan.scan_limit,
        )
        filtered = tuple(
            record for record in records if _log_record_matches_query(record, plan.query)
        )
        return filtered[-read_limit:]

    def _read_stream_records(
        self,
        streams: Iterable[str],
        *,
        limit: int,
    ) -> tuple[RedisStreamRecord, ...]:
        read_limit = _stream_read_limit(limit)
        pages = [
            (stream, self.redis.stream_reverse_range(self._stream_key(stream), count=read_limit))
            for stream in streams
            if stream
        ]
        return _latest_records(pages, limit=read_limit)

    def _stream_key(self, stream: str) -> str:
        return self.redis.key(stream)

    def _ingest_cursor_root(self, workspace_id: str) -> str:
        return f"event-log-ingest-cursors/workspaces/{event_stream_part(workspace_id)}"

    def _ingest_cursor_key(self, workspace_id: str, cursor_digest: str) -> str:
        return self.redis.key(f"{self._ingest_cursor_root(workspace_id)}/{cursor_digest}")

    def _raise_if_log_cursor_expired(
        self,
        streams: Iterable[str],
        query: LogStreamQuery,
    ) -> None:
        cursor = _log_query_cursor(query)
        if cursor is not None:
            self._raise_if_cursor_expired(streams, cursor=cursor, clamp=query.clamp)

    def _raise_if_cursor_expired(
        self,
        streams: Iterable[str],
        *,
        cursor: str,
        clamp: bool | None,
    ) -> None:
        cursor_parts = _unclamped_cursor_parts(cursor, clamp=clamp)
        if cursor_parts is None:
            return
        for stream in streams:
            if not stream:
                continue
            first_entry_id = self.redis.eval_scalar(
                _FIRST_STREAM_ENTRY_ID_SCRIPT,
                1,
                self._stream_key(stream),
            )
            _raise_if_cursor_precedes(cursor_parts, first_entry_id)


@dataclass(slots=True)
class AsyncRedisEventStreamRepository:
    redis: AsyncRedisClient
    planner: EventStreamPlanner = field(default_factory=EventStreamPlanner)
    retention: RealtimeStreamRetention = field(default_factory=RealtimeStreamRetention)

    async def append_event(
        self,
        event_type: str | EventRecordType,
        data: EventDataInput,
        *,
        event_id: str | None = None,
    ) -> CloudEventRecord:
        event, streams, body, headers = _event_append_data(
            self.planner,
            event_type,
            data,
            event_id=event_id,
        )
        stream_keys = tuple(self._stream_key(stream) for stream in streams)
        if stream_keys:
            await self.redis.eval_int(
                _APPEND_EVENT_SCRIPT,
                len(stream_keys),
                *stream_keys,
                self.retention.max_entries,
                self.retention.ttl_seconds,
                body,
                headers,
            )
        return event

    async def read_event_history(
        self,
        query: EventHistoryQuery,
        *,
        limit: int | None = None,
        cursor: str | None = None,
        clamp: bool | None = None,
    ) -> tuple[RedisStreamRecord, ...]:
        plan = self.planner.plan_event_history_read(query)
        if cursor is not None:
            await self._raise_if_cursor_expired(
                (*plan.initial_streams, *plan.fallback_streams),
                cursor=cursor,
                clamp=clamp,
            )
        normalized_cursor = _normalize_entry_id(cursor) if cursor is not None else None
        read_limit = limit or plan.read_limit
        records = _filter_event_records(
            await self._read_stream_records(plan.initial_streams, limit=read_limit),
            query=plan.query,
            cursor=normalized_cursor,
            limit=read_limit,
        )
        if records or not plan.fallback_streams:
            return records
        return _filter_event_records(
            await self._read_stream_records(plan.fallback_streams, limit=read_limit),
            query=plan.query,
            cursor=normalized_cursor,
            limit=read_limit,
        )

    async def read_logs(
        self,
        query: LogStreamQuery,
        *,
        limit: int | None = None,
    ) -> tuple[RedisStreamRecord, ...]:
        plan = self.planner.plan_log_page(query)
        await self._raise_if_log_cursor_expired(plan.streams, plan.query)
        read_limit = limit or plan.limit
        records = await self._read_stream_records(plan.streams, limit=plan.scan_limit)
        filtered = tuple(
            record for record in records if _log_record_matches_query(record, plan.query)
        )
        return filtered[-read_limit:]

    async def follow_event_history(
        self,
        tail: RedisStreamTailBroker,
        query: EventHistoryQuery,
        *,
        last_event_id: str | None = None,
        clamp: bool | None = None,
        max_events: int = 0,
        heartbeat_seconds: float,
    ) -> AsyncIterator[RedisStreamRecord | None]:
        plan = self.planner.plan_event_history_read(query)
        if last_event_id is not None:
            await self._raise_if_cursor_expired(
                plan.initial_streams,
                cursor=last_event_id,
                clamp=clamp,
            )
        return await self._follow(
            tail,
            plan.initial_streams,
            start=_normalize_entry_id(last_event_id) if last_event_id else None,
            label="events",
            max_events=max_events,
            heartbeat_seconds=heartbeat_seconds,
            skip=lambda item: event_record_headers_skip(_sequenced(item), plan.query),
        )

    async def follow_logs(
        self,
        tail: RedisStreamTailBroker,
        query: LogStreamQuery,
        *,
        last_event_id: str | None = None,
        max_events: int = 0,
        heartbeat_seconds: float,
    ) -> AsyncGenerator[RedisStreamRecord | None, None]:
        plan = self.planner.plan_log_page(query)
        if last_event_id is not None:
            await self._raise_if_cursor_expired(
                plan.streams,
                cursor=last_event_id,
                clamp=plan.query.clamp,
            )
        else:
            await self._raise_if_log_cursor_expired(plan.streams, plan.query)
        if not plan.streams:
            return _no_records()
        subscription = await tail.subscribe(
            plan.streams,
            after=dict.fromkeys(plan.streams, None),
            label="logs",
        )
        try:
            replay_query = plan.query
            if last_event_id:
                replay_query = replay_query.model_copy(update={"cursor": last_event_id})
            initial = await self.read_logs(replay_query)
        except BaseException:
            await subscription.close()
            raise
        # Subscribe before reading history so writes during the read arrive live.
        # The overlap is bounded by the page and removed by stream entry identity.
        replayed: dict[str, tuple[int, int]] = {}
        for record in initial:
            replayed[record.stream] = max(
                replayed.get(record.stream, (0, 0)), _entry_id_parts(record.entry_id)
            )
        return _followed_records(
            subscription,
            max_events=max_events,
            heartbeat_seconds=heartbeat_seconds,
            skip=lambda item: (
                _entry_id_parts(item.entry_id) <= replayed.get(item.stream, (0, 0))
                or not _log_record_matches_query(item, replay_query)
            ),
            initial=initial,
        )

    async def _follow(
        self,
        tail: RedisStreamTailBroker,
        streams: Iterable[str],
        *,
        start: str | None,
        label: str,
        max_events: int,
        heartbeat_seconds: float,
        skip: Callable[[RedisStreamRecord], bool],
    ) -> AsyncIterator[RedisStreamRecord | None]:
        stream_names = tuple(stream for stream in streams if stream)
        if not stream_names:
            return _no_records()
        subscription = await tail.subscribe(
            stream_names,
            after=dict.fromkeys(stream_names, start),
            label=label,
        )
        return _followed_records(
            subscription,
            max_events=max_events,
            heartbeat_seconds=heartbeat_seconds,
            skip=skip,
        )

    async def _read_stream_records(
        self,
        streams: Iterable[str],
        *,
        limit: int,
    ) -> tuple[RedisStreamRecord, ...]:
        read_limit = _stream_read_limit(limit)
        pages: list[tuple[str, list[RedisStreamEntry]]] = []
        for stream in streams:
            if not stream:
                continue
            entries = await self.redis.stream_reverse_range(
                self._stream_key(stream),
                count=read_limit,
            )
            pages.append((stream, entries))
        return _latest_records(pages, limit=read_limit)

    def _stream_key(self, stream: str) -> str:
        return self.redis.key(stream)

    async def _raise_if_log_cursor_expired(
        self,
        streams: Iterable[str],
        query: LogStreamQuery,
    ) -> None:
        cursor = _log_query_cursor(query)
        if cursor is not None:
            await self._raise_if_cursor_expired(streams, cursor=cursor, clamp=query.clamp)

    async def _raise_if_cursor_expired(
        self,
        streams: Iterable[str],
        *,
        cursor: str,
        clamp: bool | None,
    ) -> None:
        cursor_parts = _unclamped_cursor_parts(cursor, clamp=clamp)
        if cursor_parts is None:
            return
        for stream in streams:
            if not stream:
                continue
            first_entry_id = await self.redis.eval_scalar(
                _FIRST_STREAM_ENTRY_ID_SCRIPT,
                1,
                self._stream_key(stream),
            )
            _raise_if_cursor_precedes(cursor_parts, first_entry_id)


async def _followed_records(
    subscription: RedisStreamTailSubscription,
    *,
    max_events: int,
    heartbeat_seconds: float,
    skip: Callable[[RedisStreamRecord], bool],
    initial: tuple[RedisStreamRecord, ...] = (),
) -> AsyncGenerator[RedisStreamRecord | None, None]:
    emitted = 0
    try:
        for record in initial:
            yield record
            emitted += 1
            if max_events > 0 and emitted >= max_events:
                return
        async for item in subscription.items(heartbeat_seconds=heartbeat_seconds):
            if item is None:
                yield None
                continue
            stream, entry = item
            record = _record_from_entry(stream, entry)
            if record is None or skip(record):
                continue
            yield record
            emitted += 1
            if max_events > 0 and emitted >= max_events:
                return
    finally:
        await subscription.close()


async def _no_records() -> AsyncGenerator[RedisStreamRecord | None, None]:
    return
    yield


def _filter_event_records(
    records: Iterable[RedisStreamRecord],
    *,
    query: EventHistoryQuery,
    cursor: str | None,
    limit: int,
) -> tuple[RedisStreamRecord, ...]:
    return tuple(
        record
        for record in records
        if not event_record_headers_skip(_sequenced(record), query)
        and (cursor is None or _entry_id_parts(record.entry_id) > _entry_id_parts(cursor))
    )[:limit]


def _event_append_data(
    planner: EventStreamPlanner,
    event_type: str | EventRecordType,
    data: EventDataInput,
    *,
    event_id: str | None,
) -> tuple[CloudEventRecord, tuple[str, ...], str, str]:
    event = create_cloud_event_record(
        event_type,
        data,
        event_id=event_id or str(uuid4()),
    )
    plan = planner.append_record_for_event(event)
    return event, plan.streams, _json(plan.body), _json(plan.headers)


def _record_from_entry(stream: str, entry: RedisStreamEntry) -> RedisStreamRecord | None:
    entry_id = redis_text(entry[0])
    fields = _stream_entry_fields(entry)
    body = _load_mapping(fields.get("body"))
    headers = _load_mapping(fields.get("headers"))
    if not body and not headers:
        return None
    return RedisStreamRecord(stream=stream, entry_id=entry_id, headers=headers, body=body)


def _stream_entry_fields(entry: RedisStreamEntry) -> Mapping[str, RedisWireScalar]:
    fields = entry[1]
    return {redis_text(key): value for key, value in fields.items()}


def _container_log_script_result(
    value: RedisWireScalar | list[RedisWireScalar],
) -> tuple[int, int, int]:
    try:
        return _CONTAINER_LOG_SCRIPT_RESULT_ADAPTER.validate_python(value)
    except ValidationError as exc:
        raise RuntimeError("container log Redis script returned an invalid result") from exc


def _load_mapping(value: RedisWireScalar | None) -> Mapping[str, JsonValue]:
    if value is None:
        return {}
    try:
        return _JSON_OBJECT_ADAPTER.validate_json(redis_text(value))
    except ValidationError:
        return {}


def _json(value: Mapping[str, JsonValue]) -> str:
    return json.dumps(to_json_value(value), separators=(",", ":"), sort_keys=True)


def _is_capture_barrier(record: RedisStreamRecord) -> bool:
    """A flush carries no message; it marks the end of a batch for the ingest cursor.

    Dropped and diagnostic entries stay visible. They are the only account a reader
    gets of output the worker could not deliver, and silence would read as a container
    that printed nothing.
    """
    body = record.body
    data = body.get("data")
    payload: Mapping[str, JsonValue] = data if isinstance(data, dict) else {}
    kind = payload.get("entry_kind") or body.get("entry_kind")
    return kind == ContainerLogEntryKind.Flush.value


def log_record_from_redis(record: RedisStreamRecord) -> LogRecord:
    sequenced = _sequenced(record)
    body = dict(record.body)
    data = body.get("data")
    payload: Mapping[str, JsonValue] = data if isinstance(data, dict) else {}
    timestamp = _record_timestamp(body, payload, sequenced.seq_num)
    return LogRecord(
        id=str(body.get("id") or sequenced.headers.get("id") or record.entry_id),
        cursor=record.entry_id,
        seq_num=sequenced.seq_num,
        stored_at_ns=(
            _int_value(payload.get("stored_at_ns"))
            or _int_value(body.get("stored_at_ns"))
            or sequenced.seq_num * 1_000_000
        ),
        timestamp=timestamp,
        message=_first_text((payload, body), "message", "line"),
        stream=_first_text((payload, body), "stream"),
        container_id=_first_text(
            (payload, sequenced.headers, body),
            "container_id",
            "containerid",
        ),
        stub_id=_first_text((payload, sequenced.headers, body), "stub_id", "stubid"),
        stub_type=_first_text((payload, body), "stub_type", "stubtype"),
        task_id=_first_text((payload, sequenced.headers, body), "task_id", "taskid"),
        workspace_id=_first_text(
            (payload, sequenced.headers, body),
            "workspace_id",
            "workspaceid",
        ),
        app_id=_first_text((payload, sequenced.headers, body), "app_id", "appid"),
        deployment_id=_first_text(
            (payload, sequenced.headers, body),
            "deployment_id",
            "deploymentid",
        ),
        machine_id=_first_text(
            (payload, sequenced.headers, body),
            "machine_id",
            "machineid",
        ),
        worker_id=_first_text((payload, sequenced.headers, body), "worker_id", "workerid"),
        pid=_int_value(payload.get("pid")),
        process_args=_text_tuple(payload.get("process_args")),
        process_cwd=_first_text((payload, body), "process_cwd"),
        process_seq=_int_value(payload.get("process_seq")),
    )


def _sequenced(record: RedisStreamRecord) -> EventSequencedRecord:
    return EventSequencedRecord(
        seq_num=_entry_seq_num(record.entry_id),
        headers={str(key): str(value) for key, value in record.headers.items()},
        body=dict(record.body),
    )


def _entry_id_order(entry_id: str) -> tuple[int, int]:
    """Order a Redis-issued stream entry ID by its numeric milliseconds and sequence.

    Redis renders the sequence unpadded, so comparing entry IDs as text places
    `<ms>-10` ahead of `<ms>-7`. Every entry a single Lua script appends carries
    one frozen millisecond, so a container log batch fills the sequence far past
    9 and text order would scramble the batch it just wrote in order.
    """
    milliseconds, _separator, sequence = entry_id.partition("-")
    return (
        int(milliseconds) if milliseconds.isdigit() else 0,
        int(sequence) if sequence.isdigit() else 0,
    )


def _entry_seq_num(entry_id: str) -> int:
    return _entry_id_order(entry_id)[0]


def _record_sort_key(record: RedisStreamRecord) -> tuple[int, int, str]:
    milliseconds, sequence = _entry_id_order(record.entry_id)
    return (milliseconds, sequence, record.stream)


def _stream_read_limit(limit: int) -> int:
    return limit if limit > 0 else DEFAULT_REDIS_EVENT_STREAM_READ_LIMIT


def _latest_records(
    pages: Iterable[tuple[str, list[RedisStreamEntry]]],
    *,
    limit: int,
) -> tuple[RedisStreamRecord, ...]:
    """The newest `limit` records across streams, oldest first."""
    records = [
        record
        for stream, entries in pages
        for entry in entries
        if (record := _record_from_entry(stream, entry)) is not None
    ]
    records.sort(key=_record_sort_key)
    return tuple(records[-limit:])


def _log_record_matches_query(record: RedisStreamRecord, query: LogStreamQuery) -> bool:
    sequenced = _sequenced(record)
    if log_record_headers_skip(sequenced, query):
        return False
    if _is_capture_barrier(record):
        return False
    log_record = log_record_from_redis(record)
    if query.cursor and _entry_id_parts(record.entry_id) <= _entry_id_parts(
        _normalize_entry_id(query.cursor)
    ):
        return False
    if query.seq_num is not None and log_record.seq_num < query.seq_num:
        return False
    if (
        query.workspace_id
        and log_record.workspace_id
        and log_record.workspace_id != query.workspace_id
    ):
        return False
    for query_value, record_value in (
        (query.task_id, log_record.task_id),
        (query.container_id, log_record.container_id),
        (query.stub_id, log_record.stub_id),
        (query.app_id, log_record.app_id),
        (query.deployment_id, log_record.deployment_id),
        (query.machine_id, log_record.machine_id),
        (query.worker_id, log_record.worker_id),
    ):
        if query_value and record_value != query_value:
            return False
    if query.start_time is not None and log_record.timestamp < query.start_time:
        return False
    if query.end_time is not None and log_record.timestamp >= query.end_time:
        return False
    return not (query.query and query.query.lower() not in log_record.message.lower())


def _log_query_cursor(query: LogStreamQuery) -> str | None:
    if query.cursor:
        return query.cursor
    if query.seq_num is not None:
        return f"{query.seq_num}-0"
    return None


def _unclamped_cursor_parts(cursor: str, *, clamp: bool | None) -> tuple[int, int] | None:
    """Parsed cursor when a reader refused clamping, else `None`.

    The cursor is validated either way, so a malformed one is rejected before
    clamping could hide it.
    """
    parts = _entry_id_parts(_normalize_entry_id(cursor))
    return parts if clamp is False else None


def _raise_if_cursor_precedes(
    cursor_parts: tuple[int, int],
    first_entry_id: RedisWireScalar | None,
) -> None:
    if first_entry_id is None:
        return
    if cursor_parts < _entry_id_parts(redis_text(first_entry_id)):
        raise ExpiredCursorError("realtime cursor is older than retained history")


def _normalize_entry_id(value: str) -> str:
    text = value.strip()
    if not text:
        raise InvalidInputError("realtime cursor must not be empty")
    if text.isdigit():
        return f"{text}-0"
    _entry_id_parts(text)
    return text


def _entry_id_parts(value: str) -> tuple[int, int]:
    text = value.strip()
    head, separator, tail = text.partition("-")
    if not separator or not head.isdigit() or not tail.isdigit():
        raise InvalidInputError("realtime cursor must be a Redis stream entry ID")
    return int(head), int(tail)


def _record_timestamp(
    body: Mapping[str, JsonValue],
    payload: Mapping[str, JsonValue],
    seq_num: int,
) -> datetime:
    for value in (payload.get("timestamp"), body.get("time"), body.get("timestamp")):
        parsed = _datetime_value(value)
        if parsed is not None:
            return parsed
    if seq_num > 0:
        return datetime.fromtimestamp(seq_num / 1000, UTC)
    return datetime.now(UTC)


def _datetime_value(value: JsonValue) -> datetime | None:
    if isinstance(value, int | float):
        raw = float(value)
        if raw > 10_000_000_000:
            raw /= 1000
        return datetime.fromtimestamp(raw, UTC)
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(UTC)
    except ValueError:
        return None


def _first_text(sources: Iterable[Mapping[str, JsonValue]], *keys: str) -> str:
    for key in keys:
        for source in sources:
            value = source.get(key)
            if value is None:
                continue
            text = str(value).strip()
            if text:
                return text
    return ""


def _int_value(value: JsonValue) -> int:
    if isinstance(value, bool) or value is None:
        return 0
    if isinstance(value, int):
        return max(value, 0)
    if isinstance(value, float):
        return max(int(value), 0)
    if isinstance(value, str) and value.strip():
        try:
            return max(int(value), 0)
        except ValueError:
            return 0
    return 0


def _text_tuple(value: JsonValue) -> tuple[str, ...]:
    if isinstance(value, list | tuple):
        return tuple(str(item) for item in value)
    return ()
