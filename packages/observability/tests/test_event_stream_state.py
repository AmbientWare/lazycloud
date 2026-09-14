from __future__ import annotations

from contextlib import aclosing

import anyio
import pytest
from coordination.redis_client import AsyncRedisClient
from coordination.stream_tail import RedisStreamTailBroker
from observability.stream_state import (
    AsyncRedisEventStreamRepository,
    RedisEventStreamRepository,
)
from shared.realtime.contracts import EventRecordType
from shared.realtime.streams import EventHistoryQuery, LogStreamQuery
from tests.real_redis import RealRedisActors


@pytest.mark.anyio
async def test_redis_event_stream_repository_reads_and_follows_generic_streams(
    async_redis: AsyncRedisClient,
    stream_broker: RedisStreamTailBroker,
    real_redis_actors: RealRedisActors,
) -> None:
    repo = RedisEventStreamRepository(real_redis_actors.client())
    repo.append_event(
        EventRecordType.ContainerMetrics,
        {"workspace_id": "workspace", "stub_id": "stub", "container_id": "container"},
        event_id="unscoped-metrics",
    )
    repo.append_event(
        EventRecordType.TaskUpdated,
        {
            "workspace_id": "workspace",
            "stub_id": "stub",
            "task_id": "task",
            "status": "running",
        },
        event_id="event-task",
    )
    repo.append_event(
        EventRecordType.ContainerLog,
        {
            "workspace_id": "workspace",
            "stub_id": "stub",
            "task_id": "task",
            "container_id": "container",
            "message": "hello",
            "stream": "stdout",
        },
        event_id="event-log",
    )
    repo.append_event(
        EventRecordType.ContainerLog,
        {
            "workspace_id": "workspace",
            "stub_id": "stub",
            "container_id": "container",
            "message": "container-only",
            "stream": "stdout",
        },
        event_id="unscoped-log",
    )

    events = repo.read_event_history(
        EventHistoryQuery(workspace_id="workspace", stub_id="stub", task_id="task")
    )
    logs = repo.read_logs(LogStreamQuery(workspace_id="workspace", stub_id="stub", task_id="task"))
    followed = [
        record
        async for record in await AsyncRedisEventStreamRepository(async_redis).follow_event_history(
            stream_broker,
            EventHistoryQuery(workspace_id="workspace", stub_id="stub", task_id="task"),
            last_event_id="0-0",
            max_events=1,
            heartbeat_seconds=1.0,
        )
        if record is not None
    ]

    assert [record.body["id"] for record in events] == ["event-task"]
    assert [record.body["id"] for record in logs] == ["event-log"]
    assert followed[0].body["id"] == "event-task"


@pytest.mark.anyio
async def test_filtered_live_stream_heartbeats_during_unrelated_traffic(
    async_redis: AsyncRedisClient,
    stream_broker: RedisStreamTailBroker,
) -> None:
    repo = AsyncRedisEventStreamRepository(async_redis)
    records = await repo.follow_event_history(
        stream_broker,
        EventHistoryQuery(workspace_id="workspace", task_id="selected"),
        heartbeat_seconds=0.05,
    )
    emitted = 0

    async def publish_neighbor() -> None:
        nonlocal emitted
        while True:
            await repo.append_event(
                EventRecordType.TaskUpdated,
                {"workspace_id": "workspace", "task_id": "neighbor", "status": "running"},
            )
            emitted += 1
            await anyio.sleep(0.005)

    async with aclosing(records), anyio.create_task_group() as group:
        group.start_soon(publish_neighbor)
        with anyio.fail_after(1):
            assert await anext(records) is None
        assert emitted >= 2
        group.cancel_scope.cancel()
