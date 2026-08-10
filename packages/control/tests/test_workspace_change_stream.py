from __future__ import annotations

import threading
import time
from contextlib import ExitStack
from datetime import UTC, datetime

from api.fastapi_app import create_app
from api.server.services import ApiServices
from control.service import ControlPlaneService
from coordination.redis_client import RedisClient
from fastapi.testclient import TestClient
from observability.workspace_changes import (
    WorkspaceChangeRepository,
    WorkspaceChangeService,
)
from redis.typing import EncodableT, FieldT
from shared.http.workspace_changes import (
    WorkspaceChangeEvent,
    WorkspaceChangeTopic,
    WorkspaceChangeType,
)
from tests.redis_fakes import FakeRedis
from tests.service_fixtures import administrator_credential, owned_workspace


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
    assert '"resource_id": "default-second"' in resumed.text
    assert "default-first" not in resumed.text
    assert "tenant-only" not in resumed.text

    assert tenant_stream.status_code == 200
    assert '"workspace_id": "' + tenant.id + '"' in tenant_stream.text
    assert '"resource_id": "tenant-only"' in tenant_stream.text
    assert "default-first" not in tenant_stream.text
    assert "default-second" not in tenant_stream.text


def test_workspace_change_stream_starts_at_current_tail(
    isolated_services: ApiServices,
    client_stack: ExitStack,
) -> None:
    with isolated_services.context.database.session() as session:
        workspace_id = isolated_services.context.default_workspace_id(session)
    token, _ = administrator_credential(isolated_services, "admin")
    repository = isolated_services.workspace_changes.repository
    fake = FakeRedis()
    repository.redis = RedisClient(fake, key_prefix="test")
    repository.append(_change(workspace_id, "before-connect", event_id="event-before"))

    def publish_after_first_read() -> None:
        deadline = time.monotonic() + 2
        while not fake.xread_blocks and time.monotonic() < deadline:
            time.sleep(0.001)
        repository.append(_change(workspace_id, "after-connect", event_id="event-after"))

    publisher = threading.Thread(target=publish_after_first_read)
    publisher.start()
    client = client_stack.enter_context(TestClient(create_app(isolated_services)))

    response = client.get(
        "/api/v1/events/changes/stream?max_events=1",
        headers=_auth(token),
    )
    publisher.join(timeout=2)

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


def test_workspace_change_repository_bounds_and_deletes_workspace_streams() -> None:
    fake = FakeRedis()
    repository = WorkspaceChangeRepository(
        RedisClient(fake, key_prefix="test"),
        max_length=2,
    )
    repository.append(_change("workspace-a", "first", event_id="event-first"))
    repository.append(_change("workspace-a", "second", event_id="event-second"))
    repository.append(_change("workspace-a", "third", event_id="event-third"))
    repository.append(_change("workspace-b", "other", event_id="event-other"))

    retained = repository.read_after("workspace-a", "0-0", block_milliseconds=0)

    assert [record.event.resource_id for record in retained] == ["second", "third"]
    assert repository.delete_workspace("workspace-a") == 1
    assert repository.read_after("workspace-a", "0-0", block_milliseconds=0) == ()
    assert [
        record.event.resource_id
        for record in repository.read_after("workspace-b", "0-0", block_milliseconds=0)
    ] == ["other"]


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
