from __future__ import annotations

from observability.stream_state import RedisEventStreamRepository
from shared.realtime.contracts import EventRecordType
from shared.realtime.streams import EventHistoryQuery, LogStreamQuery
from tests.real_redis import RealRedisActors


def test_redis_event_stream_repository_reads_and_blocks_on_generic_streams(
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
    followed = list(
        repo.stream_event_history(
            EventHistoryQuery(workspace_id="workspace", stub_id="stub", task_id="task"),
            last_event_id="0-0",
            block_milliseconds=1,
            max_events=1,
        )
    )

    assert events[0].body["id"] == "event-task"
    assert logs[0].body["id"] == "event-log"
    assert followed[0].body["id"] == "event-task"
