from __future__ import annotations

from dataclasses import dataclass, field

import pytest
from lazycloud.abstractions.secret import Secret, SecretOperationError
from lazycloud.clients.secret.control import SecretControlClient
from pydantic import JsonValue
from shared.http.errors import HttpApiError
from shared.http.secrets import (
    CreateSecretResponse,
    DeleteSecretResponse,
    GetSecretResponse,
    SecretMaskedSetResponse,
    SecretWireRecord,
    UpdateSecretResponse,
)
from shared.timestamps import utc_now


@dataclass
class FakeSecretClient:
    values: dict[str, str] = field(default_factory=dict)
    fail_reads: bool = False

    def create(self, name: str, value: str) -> CreateSecretResponse:
        if name in self.values:
            raise HttpApiError("Secret already exists", status_code=409)
        self.values[name] = value
        return CreateSecretResponse(id=name, name=name)

    def get(self, name: str) -> GetSecretResponse:
        if self.fail_reads or name not in self.values:
            raise HttpApiError("Secret not found", status_code=404)
        timestamp = utc_now()
        return GetSecretResponse(
            secret=SecretWireRecord(
                id=name,
                name=name,
                value=self.values[name],
                created_at=timestamp,
                updated_at=timestamp,
            ),
        )

    def update(self, name: str, value: str) -> UpdateSecretResponse:
        if name not in self.values:
            raise HttpApiError("Secret not found", status_code=404)
        self.values[name] = value
        return UpdateSecretResponse()

    def set(self, name: str, value: str) -> SecretMaskedSetResponse:
        self.values[name] = value
        return SecretMaskedSetResponse(name=name, value="********")

    def delete(self, name: str) -> DeleteSecretResponse:
        if name not in self.values:
            raise HttpApiError("Secret not found", status_code=404)
        del self.values[name]
        return DeleteSecretResponse()


@dataclass
class RecordingSecretChannel:
    calls: list[tuple[str, str]] = field(default_factory=list)

    def get(self, path: str) -> JsonValue:
        self.calls.append(("GET", path))
        if "/full?" in path:
            return {"secrets": []}
        return {"secret": None}

    def post(self, path: str, payload: dict[str, JsonValue] | None = None) -> JsonValue:
        self.calls.append(("POST", path))
        if "/API%2FTOKEN?" in path:
            return {"name": "API/TOKEN", "value": "********"}
        return {"id": "API/TOKEN", "name": "API/TOKEN"}

    def patch(self, path: str, payload: dict[str, JsonValue] | None = None) -> JsonValue:
        self.calls.append(("PATCH", path))
        return {}

    def delete(self, path: str) -> JsonValue:
        self.calls.append(("DELETE", path))
        return {}


def test_secret_set_creates_then_updates_and_returns_records() -> None:
    client = FakeSecretClient()
    secret = Secret("API_TOKEN")._bind_control(client)

    created = secret.set("first")
    updated = secret.set("second")

    assert created.name == "API_TOKEN"
    assert created.masked() == "********"
    assert updated.value == "second"
    assert secret.get() == "second"
    assert secret.record().value == "second"


def test_secret_create_update_delete_errors_are_typed() -> None:
    client = FakeSecretClient()
    secret = Secret("API_TOKEN")._bind_control(client)

    with pytest.raises(SecretOperationError, match="not found"):
        secret.update("missing")
    with pytest.raises(SecretOperationError, match="not found"):
        secret.record()

    assert secret.create("first").value == "first"
    with pytest.raises(SecretOperationError, match="already exists"):
        secret.create("first")

    assert secret.delete() is True
    with pytest.raises(SecretOperationError, match="not found"):
        secret.delete()


def test_secret_control_client_scopes_every_request_to_selected_workspace() -> None:
    channel = RecordingSecretChannel()
    client = SecretControlClient(channel=channel, workspace="team/blue")

    client.create("API/TOKEN", "created")
    client.set("API/TOKEN", "set")
    client.list()
    client.get("API/TOKEN")
    client.update("API/TOKEN", "updated")
    client.delete("API/TOKEN")

    workspace_query = "workspace=team%2Fblue"
    assert channel.calls == [
        ("POST", f"/api/v1/secrets?{workspace_query}"),
        ("POST", f"/api/v1/secrets/API%2FTOKEN?{workspace_query}"),
        ("GET", f"/api/v1/secrets/full?{workspace_query}"),
        ("GET", f"/api/v1/secrets/API%2FTOKEN?{workspace_query}"),
        ("PATCH", f"/api/v1/secrets/API%2FTOKEN?{workspace_query}"),
        ("DELETE", f"/api/v1/secrets/API%2FTOKEN?{workspace_query}"),
    ]
