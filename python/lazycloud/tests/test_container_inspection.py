from collections.abc import Iterator

import pytest
from lazycloud.clients.resource.control import ResourceControlClient
from pydantic import JsonValue
from shared.http.errors import HttpResponseDecodeError


def test_sdk_container_client_preserves_page_request_and_rejects_environment_data() -> None:
    channel = _ContainerPageChannel()
    client = ResourceControlClient(channel)

    response = client.list_containers(limit=25, cursor="opaque-cursor")

    assert response.next == "next-cursor"
    assert [item.container.id for item in response.data] == ["container-safe"]

    channel.include_environment = True
    with pytest.raises(HttpResponseDecodeError, match="invalid response") as exc:
        client.list_containers()

    assert "must-be-rejected" not in str(exc.value)


class _ContainerPageChannel:
    def __init__(self) -> None:
        self.include_environment = False

    def get(self, path: str) -> dict[str, JsonValue]:
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
