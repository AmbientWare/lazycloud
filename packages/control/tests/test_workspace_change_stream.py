from __future__ import annotations

import threading
import time
from contextlib import ExitStack
from datetime import UTC, datetime

from api.fastapi_app import create_app
from api.server.services import ApiServices
from control.service import ControlPlaneService
from coordination.redis_client import RedisClient, redis_text
from fastapi.testclient import TestClient
from observability.workspace_changes import (
    WorkspaceChangeRepository,
    WorkspaceChangeService,
)
from pydantic import JsonValue, TypeAdapter
from redis.typing import EncodableT, FieldT
from shared.http.workspace_changes import (
    WorkspaceChangeEvent,
    WorkspaceChangeTopic,
    WorkspaceChangeType,
)
from tests.domain_fixtures import owned_workspace
from tests.real_redis import RealRedisActors
from tests.redis_fakes import FakeRedis
from tests.service_fixtures import administrator_credential

_JSON_OBJECT = TypeAdapter(dict[str, JsonValue])


def test_workspace_change_stream_requires_authentication(
    isolated_services: ApiServices,
    client_stack: ExitStack,
) -> None:
    client = client_stack.enter_context(TestClient(create_app(isolated_services)))

    response = client.get("/api/v1/events/changes/stream?max_events=1")

    assert response.status_code == 401


def test_workspace_change_stream_resumes_and_isolates_workspaces(
    isolated_services: ApiServices,
    client_stack: ExitStack,
) -> None:
    control = ControlPlaneService(isolated_services.context)
    default = owned_workspace(control, "default")
    tenant = owned_workspace(control, "tenant")
    token, _ = administrator_credential(isolated_services, "admin")
    repository = isolated_services.workspace_changes.repository
    first_default_id = repository.append(
        _change(default.id, "default-first", event_id="event-default-first")
    )
    repository.append(_change(tenant.id, "tenant-only", event_id="event-tenant"))
    second_default_id = repository.append(
        _change(default.id, "default-second", event_id="event-default-second")
    )
    client = client_stack.enter_context(TestClient(create_app(isolated_services)))

    resumed = client.get(
        "/api/v1/events/changes/stream?max_events=1",
        headers=_auth(token) | {"Last-Event-ID": first_default_id},
    )
    tenant_stream = client.get(
        f"/api/v1/events/changes/stream?workspace={tenant.id}&max_events=1",
        headers=_auth(token) | {"Last-Event-ID": "0-0"},
    )

    assert resumed.status_code == 200
    assert resumed.headers["cache-control"] == "no-cache"
    assert resumed.headers["x-accel-buffering"] == "no"
    assert resumed.headers["content-type"].startswith("text/event-stream")
    assert f"id: {second_default_id}" in resumed.text
    assert "event: workspace.change" in resumed.text
    assert [
        (event["workspace_id"], event["resource_id"]) for event in _sse_payloads(resumed.text)
    ] == [(default.id, "default-second")]

    assert tenant_stream.status_code == 200
    assert [
        (event["workspace_id"], event["resource_id"]) for event in _sse_payloads(tenant_stream.text)
    ] == [(tenant.id, "tenant-only")]


def test_workspace_change_stream_starts_at_current_tail(
    isolated_services: ApiServices,
    client_stack: ExitStack,
) -> None:
    with isolated_services.context.database.session() as session:
        workspace_id = isolated_services.context.default_workspace_id(session)
    token, _ = administrator_credential(isolated_services, "admin")
    repository = isolated_services.workspace_changes.repository
    repository.append(_change(workspace_id, "before-connect", event_id="event-before"))
    broker = isolated_services.require_async_io().realtime

    # The broker registers the subscriber, and with it the live barrier, before
    # the response starts, so a change appended once the subscriber count shows
    # up is the first thing the stream can see.
    def publish_after_subscribed() -> None:
        deadline = time.monotonic() + 5
        while (
            broker.status().subscribers.get("workspace-changes", 0) == 0
            and time.monotonic() < deadline
        ):
            time.sleep(0.001)
        assert broker.status().subscribers.get("workspace-changes", 0) == 1
        repository.append(_change(workspace_id, "after-connect", event_id="event-after"))

    publisher = threading.Thread(target=publish_after_subscribed)
    publisher.start()
    client = client_stack.enter_context(TestClient(create_app(isolated_services)))

    response = client.get(
        "/api/v1/events/changes/stream?max_events=1",
        headers=_auth(token),
    )
    publisher.join(timeout=5)

    assert not publisher.is_alive()
    assert response.status_code == 200
    assert "after-connect" in response.text
    assert "before-connect" not in response.text


def test_workspace_change_stream_rejects_invalid_resume_cursor(
    isolated_services: ApiServices,
    client_stack: ExitStack,
) -> None:
    with isolated_services.context.database.session() as session:
        isolated_services.context.default_workspace_id(session)
    token, _ = administrator_credential(isolated_services, "admin")
    client = client_stack.enter_context(TestClient(create_app(isolated_services)))

    response = client.get(
        "/api/v1/events/changes/stream?max_events=1",
        headers=_auth(token) | {"Last-Event-ID": "not-a-stream-id"},
    )

    assert response.status_code == 400
    assert response.json() == {
        "detail": "Last-Event-ID must be a Redis stream entry id",
        "code": "invalid_input",
    }


def test_workspace_change_repository_bounds_and_deletes_workspace_streams(
    real_redis_actors: RealRedisActors,
) -> None:
    redis = real_redis_actors.client()
    repository = WorkspaceChangeRepository(redis, max_length=25)
    for index in range(225):
        repository.append(
            _change(
                "workspace-a",
                f"resource-{index}",
                event_id=f"event-{index}",
            )
        )
    repository.append(_change("workspace-b", "other", event_id="event-other"))

    retained = _resource_ids(redis, repository, "workspace-a")

    assert 0 < len(retained) <= 125
    assert retained[-1] == "resource-224"
    assert repository.delete_workspace("workspace-a") == 1
    assert _resource_ids(redis, repository, "workspace-a") == []
    assert _resource_ids(redis, repository, "workspace-b") == ["other"]


def test_workspace_change_publication_failure_is_nonfatal() -> None:
    service = WorkspaceChangeService(
        WorkspaceChangeRepository(RedisClient(_FailingStreamRedis(), key_prefix="test"))
    )

    published = service.emit_change(
        workspace_id="workspace-a",
        topic=WorkspaceChangeTopic.Apps,
        change=WorkspaceChangeType.Updated,
        resource_id="app-a",
    )

    assert published is None


def _change(workspace_id: str, resource_id: str, *, event_id: str) -> WorkspaceChangeEvent:
    return WorkspaceChangeEvent(
        event_id=event_id,
        occurred_at=datetime(2026, 7, 13, 12, tzinfo=UTC),
        workspace_id=workspace_id,
        topic=WorkspaceChangeTopic.Apps,
        change=WorkspaceChangeType.Updated,
        resource_id=resource_id,
    )


def _resource_ids(
    redis: RedisClient,
    repository: WorkspaceChangeRepository,
    workspace_id: str,
) -> list[str]:
    key = repository.stream_key(workspace_id)
    return [
        WorkspaceChangeEvent.model_validate_json(redis_text(fields["event"])).resource_id
        for _key, entries in redis.stream_read({key: "0-0"}, count=1_000)
        for _entry_id, fields in entries
    ]


def _sse_payloads(body: str) -> list[dict[str, JsonValue]]:
    return [
        _JSON_OBJECT.validate_json(line.removeprefix("data: "))
        for line in body.splitlines()
        if line.startswith("data: ")
    ]


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


class _FailingStreamRedis(FakeRedis):
    def xadd(
        self,
        name: str,
        fields: dict[FieldT, EncodableT],
        *,
        id: str = "*",
        maxlen: int | None = None,
        approximate: bool = False,
    ) -> str:
        _ = name, fields, id, maxlen, approximate
        raise ConnectionError("Redis unavailable")
