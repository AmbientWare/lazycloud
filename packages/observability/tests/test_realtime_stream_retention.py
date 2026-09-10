from __future__ import annotations

from collections.abc import AsyncIterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack

import pytest
from api.fastapi_app import create_app
from api.server.async_io import ApiAsyncIo
from api.server.services import ApiServices
from coordination.redis_client import RedisClient
from fastapi.testclient import TestClient
from observability.stream_state import (
    AsyncRedisEventStreamRepository,
    RealtimeStreamRetention,
    RedisEventStreamRepository,
    RedisStreamRecord,
)
from pydantic import JsonValue
from shared.errors import ExpiredCursorError
from shared.realtime.contracts import (
    EventRecordType,
    create_cloud_event_record,
)
from shared.realtime.streams import EventHistoryQuery, LogStreamQuery
from tests.real_redis import RealRedisActors
from tests.workspaces import administrator_credential


@pytest.fixture
async def async_io(isolated_services: ApiServices) -> AsyncIterator[ApiAsyncIo]:
    io = isolated_services.require_async_io()
    await io.start()
    try:
        yield io
    finally:
        await io.close()


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
            _log_data("batch-workspace", "batch-container", index),
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


@pytest.mark.anyio
async def test_real_redis_expired_cursors_clamp_or_raise_typed_conflict(
    async_io: ApiAsyncIo,
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
        async_io.redis,
        retention=retention,
    )
    followed_log = await _first_record(
        await async_repository.follow_logs(
            async_io.realtime,
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
            async_io.realtime,
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
            async_io.realtime,
            event_query,
            last_event_id=old_event_cursor,
            clamp=False,
            max_events=1,
            heartbeat_seconds=1.0,
        )


def test_api_maps_expired_event_cursor_to_409(
    isolated_services: ApiServices,
    real_redis_actors: RealRedisActors,
    client_stack: ExitStack,
) -> None:
    redis = real_redis_actors.client()
    repository = RedisEventStreamRepository(
        redis,
        retention=RealtimeStreamRetention(ttl_seconds=30, max_entries=25),
    )
    with isolated_services.context.database.session() as session:
        workspace_id = isolated_services.context.default_workspace_id(session)

    repository.append_event(
        EventRecordType.TaskUpdated,
        {"workspace_id": workspace_id, "task_id": "api-task", "status": "running"},
        event_id="api-old-event",
    )
    event_query = EventHistoryQuery(workspace_id=workspace_id, task_id="api-task")
    old_event_cursor = repository.read_event_history(event_query)[-1].entry_id
    for index in range(1, 226):
        repository.append_event(
            EventRecordType.TaskUpdated,
            {"workspace_id": workspace_id, "task_id": "api-task", "status": "running"},
            event_id=f"api-event-{index}",
        )

    client = client_stack.enter_context(TestClient(create_app(isolated_services)))
    token, _record = administrator_credential(isolated_services.context, "cursor-admin")
    headers = {"Authorization": f"Bearer {token}"}
    event_response = client.get(
        "/api/v1/events/tasks/api-task/stream",
        params={
            "workspace": workspace_id,
            "cursor": old_event_cursor,
            "clamp": "false",
            "follow": "true",
            "max_events": 1,
        },
        headers=headers,
    )

    assert event_response.status_code == 409
    assert event_response.json() == {
        "detail": "realtime cursor is older than retained history",
        "code": "expired_cursor",
    }


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
