from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack

import pytest
from api.fastapi_app import create_app
from api.server.services import ApiServices
from coordination.redis_client import RedisClient
from fastapi.testclient import TestClient
from observability.stream_state import (
    RealtimeStreamRetention,
    RedisEventStreamRepository,
)
from pydantic import JsonValue
from shared.errors import ExpiredCursorError
from shared.realtime.contracts import (
    EventRecordType,
    create_cloud_event_record,
)
from shared.realtime.streams import EventHistoryQuery, LogStreamQuery
from tests.real_redis import RealRedisActors
from tests.service_fixtures import administrator_credential


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


def test_real_redis_expired_cursors_clamp_or_raise_typed_conflict(
    real_redis_actors: RealRedisActors,
) -> None:
    redis = real_redis_actors.client()
    repository = RedisEventStreamRepository(
        redis,
        retention=RealtimeStreamRetention(ttl_seconds=30, max_entries=25),
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
    followed_logs = tuple(
        repository.stream_logs(
            LogStreamQuery(workspace_id=workspace_id, cursor=old_log_cursor),
            block_milliseconds=1,
            max_events=1,
        )
    )
    assert followed_logs[0].entry_id == clamped_logs[0].entry_id
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
    followed_events = tuple(
        repository.stream_event_history(
            event_query,
            last_event_id=old_event_cursor,
            block_milliseconds=1,
            max_events=1,
        )
    )
    assert followed_events[0].entry_id == clamped_events[0].entry_id
    with pytest.raises(
        ExpiredCursorError,
        match="realtime cursor is older than retained history",
    ):
        repository.stream_event_history(
            event_query,
            last_event_id=old_event_cursor,
            clamp=False,
            block_milliseconds=1,
            max_events=1,
        )


def test_api_maps_expired_log_and_event_cursors_to_409(
    isolated_services: ApiServices,
    real_redis_actors: RealRedisActors,
    request: pytest.FixtureRequest,
    client_stack: ExitStack,
) -> None:
    redis = real_redis_actors.client()
    services = _services_with_redis(isolated_services, redis, request)
    repository = RedisEventStreamRepository(
        redis,
        retention=RealtimeStreamRetention(ttl_seconds=30, max_entries=25),
    )
    with services.context.database.session() as session:
        workspace_id = services.context.default_workspace_id(session)

    repository.append_event(
        EventRecordType.ContainerLog,
        _log_data(workspace_id, "api-container", 0),
        event_id="api-old-log",
    )
    old_log_cursor = repository.read_logs(LogStreamQuery(workspace_id=workspace_id))[-1].entry_id
    repository.append_event(
        EventRecordType.TaskUpdated,
        {"workspace_id": workspace_id, "task_id": "api-task", "status": "running"},
        event_id="api-old-event",
    )
    event_query = EventHistoryQuery(workspace_id=workspace_id, task_id="api-task")
    old_event_cursor = repository.read_event_history(event_query)[-1].entry_id
    for index in range(1, 226):
        repository.append_event(
            EventRecordType.ContainerLog,
            _log_data(workspace_id, "api-container", index),
            event_id=f"api-log-{index}",
        )
        repository.append_event(
            EventRecordType.TaskUpdated,
            {"workspace_id": workspace_id, "task_id": "api-task", "status": "running"},
            event_id=f"api-event-{index}",
        )

    client = client_stack.enter_context(TestClient(create_app(services)))
    token, _record = administrator_credential(isolated_services, "cursor-admin")
    headers = {"Authorization": f"Bearer {token}"}
    log_response = client.get(
        "/api/v1/logs",
        params={
            "workspace": workspace_id,
            "cursor": old_log_cursor,
            "clamp": "false",
        },
        headers=headers,
    )
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

    assert log_response.status_code == 409
    assert log_response.json() == {
        "detail": "realtime cursor is older than retained history",
        "code": "expired_cursor",
    }
    assert event_response.status_code == 409
    assert event_response.json() == {
        "detail": "realtime cursor is older than retained history",
        "code": "expired_cursor",
    }


def _services_with_redis(
    isolated_services: ApiServices,
    redis: RedisClient,
    request: pytest.FixtureRequest,
) -> ApiServices:
    services = ApiServices.create(
        isolated_services.database,
        root=isolated_services.root,
        create_schema=False,
        volume_filesystem=isolated_services.volume_filesystem,
        redis_client=redis,
        binary_redis_client=isolated_services.binary_redis_client,
        owns_redis_client=False,
        owns_binary_redis_client=False,
    )
    request.addfinalizer(services.close)
    return services


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
