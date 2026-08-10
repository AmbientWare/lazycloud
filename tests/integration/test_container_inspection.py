from __future__ import annotations

from collections.abc import Iterator
from contextlib import ExitStack
from datetime import UTC, datetime
from uuid import NAMESPACE_URL, uuid5

import pytest
from api.fastapi_app import create_app
from api.server.services import ApiServices
from control.service import ControlPlaneService
from database.repositories.orchestration import ContainerRepository
from database.tables.orchestration import ContainerTable
from fastapi.testclient import TestClient
from identity.auth import AuthService
from lazycloud.clients.resource.control import ResourceControlClient
from pydantic import JsonValue
from shared.containers import ContainerRecord, ContainerStatus
from shared.http.compute import ContainerWithAppPageResponse
from shared.http.errors import HttpResponseDecodeError
from shared.identity import TokenKind
from sqlalchemy import update
from tests.service_fixtures import owned_workspace


@pytest.fixture
def client_stack() -> Iterator[ExitStack]:
    with ExitStack() as stack:
        yield stack


def test_canonical_container_pages_are_bounded_stable_and_secret_free(
    isolated_services: ApiServices,
    client_stack: ExitStack,
) -> None:
    workspace_id = ControlPlaneService(isolated_services.context).get_workspace().id
    marker = "container-inspection-secret-marker"
    created_at = datetime(2026, 7, 12, 12, tzinfo=UTC)
    expected_ids = [
        str(uuid5(NAMESPACE_URL, f"lazycloud:gateway-container:{index}")) for index in range(205)
    ]
    foreign_workspace = owned_workspace(
        ControlPlaneService(isolated_services.context), "container-inspection-foreign"
    )
    with isolated_services.context.database.session() as session:
        repository = ContainerRepository(session)
        for index, container_id in enumerate(expected_ids):
            repository.upsert(
                ContainerRecord(
                    id=container_id,
                    name=f"gateway-container-{index:03d}",
                    image="python:3.12",
                    command=["python", "worker.py"],
                    workspace_id=workspace_id,
                    status=ContainerStatus.Running,
                    env={"GATEWAY_TOKEN": marker, "ORDINARY_VALUE": "not-public"},
                    created_at=created_at,
                )
            )
        repository.upsert(
            ContainerRecord(
                id=str(uuid5(NAMESPACE_URL, "lazycloud:gateway-container:foreign")),
                name="foreign-container",
                image="python:3.12",
                command=[],
                workspace_id=foreign_workspace.id,
                status=ContainerStatus.Running,
                env={"GATEWAY_TOKEN": "foreign-secret-marker"},
                created_at=created_at,
            )
        )
        session.execute(
            update(ContainerTable)
            .where(ContainerTable.id.in_(expected_ids))
            .values(created_at=created_at)
        )

    token, _ = AuthService(isolated_services.context).create_token(
        "container-inspection",
        kind=TokenKind.Workspace,
    )
    headers = {"Authorization": f"Bearer {token}"}
    client = client_stack.enter_context(TestClient(create_app(isolated_services)))

    cursor = ""
    received_ids: list[str] = []
    page_sizes: list[int] = []
    seen_cursors: set[str] = set()
    while True:
        response = client.get(
            "/api/v1/containers",
            headers=headers,
            params={"limit": 100, "cursor": cursor, "status": "running"},
        )
        assert response.status_code == 200, response.text
        assert marker not in response.text
        assert "GATEWAY_TOKEN" not in response.text
        payload = ContainerWithAppPageResponse.model_validate_json(response.content)
        assert set(type(payload).model_fields) == {"data", "next"}
        page_ids = [item.container.id for item in payload.data]
        page_sizes.append(len(page_ids))
        received_ids.extend(page_ids)
        cursor = payload.next
        if not cursor:
            break
        assert cursor not in seen_cursors
        seen_cursors.add(cursor)

    assert page_sizes == [100, 100, 5]
    assert received_ids == sorted(expected_ids, reverse=True)
    assert len(received_ids) == len(set(received_ids)) == 205

    oversized = client.get(
        "/api/v1/containers",
        headers=headers,
        params={"limit": 101},
    )
    invalid_cursor = client.get(
        "/api/v1/containers",
        headers=headers,
        params={"cursor": "not-a-cursor"},
    )
    forged_workspace = client.get(
        "/api/v1/containers",
        headers=headers,
        params={"workspace": foreign_workspace.id},
    )
    assert oversized.status_code == 422
    assert invalid_cursor.status_code == 400
    assert forged_workspace.status_code == 403


def test_sdk_container_client_preserves_page_request_and_rejects_environment_data() -> None:
    channel = _ContainerPageChannel()
    client = ResourceControlClient(channel)

    response = client.list_containers(limit=25, cursor="opaque-cursor")

    assert response.next == "next-cursor"
    assert [item.container.id for item in response.data] == ["container-safe"]
    assert channel.gets == ["/api/v1/containers?workspace=default&limit=25&cursor=opaque-cursor"]

    channel.include_environment = True
    with pytest.raises(HttpResponseDecodeError, match="invalid response") as exc:
        client.list_containers()

    assert "must-be-rejected" not in str(exc.value)


class _ContainerPageChannel:
    def __init__(self) -> None:
        self.gets: list[str] = []
        self.include_environment = False

    def get(self, path: str) -> dict[str, JsonValue]:
        self.gets.append(path)
        item: dict[str, JsonValue] = {
            "id": "container-safe",
            "name": "safe",
            "image": "python:3.12",
            "command": [],
            "workspace_id": "workspace-1",
            "status": "running",
            "ports": {},
            "created_at": "2026-07-12T12:00:00Z",
        }
        if self.include_environment:
            item["env"] = {"GATEWAY_TOKEN": "must-be-rejected"}
        return {"data": [{"container": item, "app_id": ""}], "next": "next-cursor"}

    def post(
        self,
        path: str,
        payload: dict[str, JsonValue] | None = None,
    ) -> dict[str, JsonValue]:
        raise AssertionError(f"unexpected POST {path}: {payload}")

    def request(
        self,
        method: str,
        path: str,
        *,
        payload: dict[str, JsonValue] | None = None,
    ) -> dict[str, JsonValue]:
        raise AssertionError(f"unexpected {method} {path}: {payload}")

    def stream_get(self, path: str) -> Iterator[str]:
        raise AssertionError(f"unexpected stream GET {path}")
