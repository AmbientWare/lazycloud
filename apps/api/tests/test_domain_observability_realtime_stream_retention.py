from __future__ import annotations

from contextlib import ExitStack

from api.fastapi_app import create_app
from api.server.services import ApiServices
from fastapi.testclient import TestClient
from observability.stream_state import (
    RealtimeStreamRetention,
    RedisEventStreamRepository,
)
from shared.realtime.contracts import (
    EventRecordType,
)
from shared.realtime.streams import EventHistoryQuery
from tests.real_redis import RealRedisActors
from tests.workspaces import administrator_credential


def test_api_maps_expired_event_cursor_to_409(
    isolated_services: ApiServices,
    real_redis_actors: RealRedisActors,
) -> None:
    with ExitStack() as client_stack:
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
