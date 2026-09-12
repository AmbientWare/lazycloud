from __future__ import annotations

from collections.abc import AsyncIterator
from concurrent.futures import ThreadPoolExecutor

import pytest
from coordination.redis_client import AsyncRedisClient, RedisClient
from coordination.stream_tail import RedisStreamTailBroker
from observability.stream_state import (
    AsyncRedisEventStreamRepository,
    RealtimeStreamRetention,
    RedisEventStreamRepository,
    RedisStreamRecord,
    log_record_from_redis,
)
from pydantic import JsonValue
from shared.errors import ExpiredCursorError
from shared.realtime.contracts import (
    EventRecordType,
    create_cloud_event_record,
)
from shared.realtime.streams import EventHistoryQuery, LogStreamQuery
from tests.real_redis import RealRedisActors


def test_real_redis_single_and_batch_appends_bound_every_stream_and_cleanup(
    real_redis_actors: RealRedisActors,
) -> None:
    redis = real_redis_actors.client()
    retention = RealtimeStreamRetention(ttl_seconds=30, max_entries=25)
    repository = RedisEventStreamRepository(redis, retention=retention)

    for index in range(225):
        repository.append_event(
            EventRecordType.ContainerLog,
            _log_data("single-workspace", "single-container", index),
            event_id=f"single-{index}",
        )

    single_streams = _workspace_stream_keys(redis, "single-workspace")
    assert single_streams
    _assert_stream_bounds(redis, single_streams, retention)

    events = tuple(
        create_cloud_event_record(
            EventRecordType.ContainerLog,
            {
                **_log_data("batch-workspace", "batch-container", index),
                "capture_id": "capture-1",
                "source_sequence": index,
            },
            event_id=f"batch-{index}",
        )
        for index in range(225)
    )
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = tuple(
            executor.submit(
                repository.append_container_log_batch,
                container_id="batch-container",
                capture_id="capture-1",
                first_sequence=0,
                events=events,
            )
            for _index in range(2)
        )
        outcomes = tuple(future.result() for future in futures)

    assert sorted(outcome.appended_count for outcome in outcomes) == [0, len(events)]
    assert all(not outcome.sequence_gap for outcome in outcomes)
    batch_streams = _workspace_stream_keys(redis, "batch-workspace")
    assert batch_streams
    _assert_stream_bounds(redis, batch_streams, retention)

    all_batch_keys = set(redis.scan(f"{redis.key_prefix}:*batch-workspace*"))
    cursor_keys = {key for key in all_batch_keys if "event-log-ingest-cursors/workspaces" in key}
    assert len(cursor_keys) == 1
    assert all(1 <= redis.ttl(key) <= retention.ttl_seconds for key in cursor_keys)

    for key in single_streams:
        assert redis.expire(key, 1)
    repository.append_event(
        EventRecordType.ContainerLog,
        _log_data("single-workspace", "single-container", 226),
        event_id="single-refresh",
    )
    assert all(redis.ttl(key) == retention.ttl_seconds for key in single_streams)

    assert repository.delete_workspace("batch-workspace") == len(all_batch_keys)
    assert redis.scan(f"{redis.key_prefix}:*batch-workspace*") == []
    assert all(redis.exists(key) for key in single_streams)


def test_capture_cursor_recovers_retained_sequences_and_starts_a_new_retention_window(
    real_redis_actors: RealRedisActors,
) -> None:
    redis = real_redis_actors.client()
    repository = RedisEventStreamRepository(redis)
    workspace_id = "capture-recovery-workspace"
    container_id = "capture-recovery-container"
    capture_id = "capture-recovery"
    events = tuple(
        create_cloud_event_record(
            EventRecordType.ContainerLog,
            {
                **_log_data(workspace_id, container_id, sequence),
                "capture_id": capture_id,
                "source_sequence": sequence,
            },
            event_id=f"capture-recovery-{sequence}",
        )
        for sequence in range(5)
    )
    repository.append_container_log_batch(
        container_id=container_id, capture_id=capture_id, first_sequence=0, events=events[:2]
    )
    (cursor_key,) = redis.scan(
        f"{redis.key_prefix}:*event-log-ingest-cursors/workspaces/{workspace_id}/*"
    )
    repository.append_container_log_batch(
        container_id=container_id,
        capture_id="other-capture",
        first_sequence=0,
        events=tuple(
            create_cloud_event_record(
                EventRecordType.ContainerLog,
                {
                    **_log_data(workspace_id, container_id, sequence),
                    "capture_id": "other-capture",
                    "source_sequence": sequence,
                },
                event_id=f"other-capture-{sequence}",
            )
            for sequence in range(129)
        ),
    )
    assert redis.delete(cursor_key) == 1
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = tuple(
            executor.submit(
                repository.append_container_log_batch,
                container_id=container_id,
                capture_id=capture_id,
                first_sequence=1,
                events=events[1:3],
            )
            for _ in range(2)
        )
        outcomes = tuple(future.result() for future in futures)
    assert sorted(outcome.appended_count for outcome in outcomes) == [0, 1]
    assert all(outcome.accepted_through == 2 and not outcome.sequence_gap for outcome in outcomes)
    records = repository.read_logs(LogStreamQuery(workspace_id=workspace_id, limit=200))
    assert [
        record.body["id"] for record in records if record.body["id"] in {e.id for e in events}
    ] == [event.id for event in events[:3]]

    assert redis.delete(cursor_key) == 1
    gap = repository.append_container_log_batch(
        container_id=container_id, capture_id=capture_id, first_sequence=4, events=events[4:]
    )
    assert gap.sequence_gap and gap.accepted_through == 2 and gap.appended_count == 0

    repository.delete_workspace(workspace_id)
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = tuple(
            executor.submit(
                repository.append_container_log_batch,
                container_id=container_id,
                capture_id=capture_id,
                first_sequence=3,
                events=events[3:],
            )
            for _ in range(2)
        )
        outcomes = tuple(future.result() for future in futures)
    assert sorted(outcome.appended_count for outcome in outcomes) == [0, 2]
    assert all(outcome.accepted_through == 4 and not outcome.sequence_gap for outcome in outcomes)
    records = repository.read_logs(LogStreamQuery(workspace_id=workspace_id))
    assert len(records) == 3
    diagnostic = records[0].body["data"]
    assert isinstance(diagnostic, dict)
    assert diagnostic["entry_kind"] == "diagnostic"
    assert diagnostic["stream"] == "system"
    assert "source_sequence" not in diagnostic
    assert [log_record_from_redis(record).message for record in records[1:]] == ["line-3", "line-4"]
    assert [record.body["id"] for record in records[1:]] == [event.id for event in events[3:]]


@pytest.mark.anyio
async def test_real_redis_expired_cursors_clamp_or_raise_typed_conflict(
    async_redis: AsyncRedisClient,
    stream_broker: RedisStreamTailBroker,
    real_redis_actors: RealRedisActors,
) -> None:
    redis = real_redis_actors.client()
    retention = RealtimeStreamRetention(ttl_seconds=30, max_entries=25)
    repository = RedisEventStreamRepository(
        redis,
        retention=retention,
    )
    workspace_id = "cursor-workspace"

    repository.append_event(
        EventRecordType.ContainerLog,
        _log_data(workspace_id, "log-container", 0),
        event_id="old-log",
    )
    old_log_cursor = repository.read_logs(LogStreamQuery(workspace_id=workspace_id))[-1].entry_id
    for index in range(1, 226):
        repository.append_event(
            EventRecordType.ContainerLog,
            _log_data(workspace_id, "log-container", index),
            event_id=f"log-{index}",
        )

    clamped_logs = repository.read_logs(
        LogStreamQuery(workspace_id=workspace_id, cursor=old_log_cursor)
    )
    assert clamped_logs
    assert clamped_logs[0].entry_id != old_log_cursor
    async_repository = AsyncRedisEventStreamRepository(
        async_redis,
        retention=retention,
    )
    followed_log = await _first_record(
        await async_repository.follow_logs(
            stream_broker,
            LogStreamQuery(workspace_id=workspace_id, cursor=old_log_cursor),
            max_events=1,
            heartbeat_seconds=1.0,
        )
    )
    assert followed_log.entry_id == clamped_logs[0].entry_id
    with pytest.raises(
        ExpiredCursorError,
        match="realtime cursor is older than retained history",
    ):
        repository.read_logs(
            LogStreamQuery(
                workspace_id=workspace_id,
                cursor=old_log_cursor,
                clamp=False,
            )
        )

    event_query = EventHistoryQuery(workspace_id=workspace_id, task_id="event-task")
    repository.append_event(
        EventRecordType.TaskUpdated,
        {"workspace_id": workspace_id, "task_id": "event-task", "status": "running"},
        event_id="old-event",
    )
    old_event_cursor = repository.read_event_history(event_query)[-1].entry_id
    for index in range(1, 226):
        repository.append_event(
            EventRecordType.TaskUpdated,
            {"workspace_id": workspace_id, "task_id": "event-task", "status": "running"},
            event_id=f"event-{index}",
        )

    clamped_events = repository.read_event_history(
        event_query,
        cursor=old_event_cursor,
    )
    assert clamped_events
    assert clamped_events[0].entry_id != old_event_cursor
    followed_event = await _first_record(
        await async_repository.follow_event_history(
            stream_broker,
            event_query,
            last_event_id=old_event_cursor,
            max_events=1,
            heartbeat_seconds=1.0,
        )
    )
    assert followed_event.entry_id == clamped_events[0].entry_id
    with pytest.raises(
        ExpiredCursorError,
        match="realtime cursor is older than retained history",
    ):
        await async_repository.follow_event_history(
            stream_broker,
            event_query,
            last_event_id=old_event_cursor,
            clamp=False,
            max_events=1,
            heartbeat_seconds=1.0,
        )


async def _first_record(records: AsyncIterator[RedisStreamRecord | None]) -> RedisStreamRecord:
    async for record in records:
        if record is not None:
            return record
    raise AssertionError("followed stream ended without a record")


def _log_data(workspace_id: str, container_id: str, index: int) -> dict[str, JsonValue]:
    return {
        "workspace_id": workspace_id,
        "stub_id": "stub-1",
        "app_id": "app-1",
        "task_id": "log-task",
        "container_id": container_id,
        "message": f"line-{index}",
        "stream": "stdout",
        "timestamp": "2026-07-19T00:00:00Z",
    }


def _workspace_stream_keys(redis: RedisClient, workspace_id: str) -> tuple[str, ...]:
    return tuple(
        key
        for key in redis.scan(f"{redis.key_prefix}:*{workspace_id}*")
        if "event-log-ingest-cursors/workspaces" not in key
    )


def _assert_stream_bounds(
    redis: RedisClient,
    stream_keys: tuple[str, ...],
    retention: RealtimeStreamRetention,
) -> None:
    for key in stream_keys:
        length = redis.eval_int("return redis.call('XLEN', KEYS[1])", 1, key)
        assert 0 < length <= retention.max_entries + 100
        assert 1 <= redis.ttl(key) <= retention.ttl_seconds
