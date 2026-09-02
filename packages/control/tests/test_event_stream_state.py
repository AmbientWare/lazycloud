from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from api.server.async_io import ApiAsyncIo
from api.server.services import ApiServices
from observability.stream_state import (
    AsyncRedisEventStreamRepository,
    RedisEventStreamRepository,
)
from shared.realtime.contracts import EventRecordType
from shared.realtime.streams import EventHistoryQuery, LogStreamQuery
from tests.real_redis import RealRedisActors


@pytest.fixture
async def async_io(isolated_services: ApiServices) -> AsyncIterator[ApiAsyncIo]:
    io = isolated_services.require_async_io()
    await io.start()
    try:
        yield io
    finally:
        await io.close()


@pytest.mark.anyio
async def test_redis_event_stream_repository_reads_and_follows_generic_streams(
    async_io: ApiAsyncIo,
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
        async for record in await AsyncRedisEventStreamRepository(
            async_io.redis
        ).follow_event_history(
            async_io.realtime,
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
