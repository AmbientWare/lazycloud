from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

import pytest
from lazycloud.clients.resource.control import ResourceControlClient
from pydantic import JsonValue
from shared.app_lifecycle import AppLifecycleState
from shared.http.apps import AppResponse
from shared.http.errors import HttpApiError, HttpResponseDecodeError, HttpTransportError

_NOW = datetime(2026, 7, 20, tzinfo=UTC)


@dataclass
class RecordingChannel:
    responses: list[JsonValue]
    calls: list[tuple[str, str, dict[str, JsonValue] | None]] = field(default_factory=list)

    def get(self, path: str) -> JsonValue:
        self.calls.append(("GET", path, None))
        return self.responses.pop(0)

    def post(self, path: str, payload: dict[str, JsonValue] | None = None) -> JsonValue:
        self.calls.append(("POST", path, payload))
        return self.responses.pop(0)

    def request(
        self,
        method: str,
        path: str,
        *,
        payload: dict[str, JsonValue] | None = None,
    ) -> JsonValue:
        self.calls.append((method, path, payload))
        return self.responses.pop(0) if self.responses else None


def _app(state: AppLifecycleState = AppLifecycleState.Active) -> AppResponse:
    return AppResponse(
        id="app-1",
        workspace_id="workspace-1",
        name="demo",
        lifecycle_state=state,
        active=state is AppLifecycleState.Active,
        created_at=_NOW,
        updated_at=_NOW,
    )


def test_resource_client_percent_encodes_path_identifiers() -> None:
    active = _app()
    channel = RecordingChannel(responses=[active.model_dump(mode="json")])
    client = ResourceControlClient(channel=channel, workspace="workspace/one")
    identifier = "nested/name?draft#one"

    assert client.app(identifier) == active
    client.delete_app(identifier)

    encoded = "nested%2Fname%3Fdraft%23one"
    assert channel.calls == [
        ("GET", f"/api/v1/apps/{encoded}?workspace=workspace%2Fone", None),
        ("DELETE", f"/api/v1/apps/{encoded}?workspace=workspace%2Fone", None),
    ]


def test_resource_client_raises_typed_decode_error() -> None:
    client = ResourceControlClient(
        channel=RecordingChannel(responses=[{"unexpected": "shape"}]),
        workspace="workspace-1",
    )

    with pytest.raises(HttpResponseDecodeError, match="invalid response"):
        client.app("app-1")


@pytest.mark.parametrize(
    "failure",
    [
        HttpApiError("denied", status_code=403),
        HttpTransportError("GET", "https://example.com/api/v1/apps/app-1", "offline"),
    ],
)
def test_resource_client_preserves_request_failures(failure: RuntimeError) -> None:
    class FailingChannel(RecordingChannel):
        def get(self, path: str) -> JsonValue:
            _ = path
            raise failure

    client = ResourceControlClient(channel=FailingChannel(responses=[]), workspace="workspace-1")

    with pytest.raises(type(failure)) as raised:
        client.app("app-1")
    assert raised.value is failure
