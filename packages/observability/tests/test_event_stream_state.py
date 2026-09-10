from __future__ import annotations

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

    assert events[0].body["id"] == "event-task"
    assert logs[0].body["id"] == "event-log"
    assert followed[0].body["id"] == "event-task"
