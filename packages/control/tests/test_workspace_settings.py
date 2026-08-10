from __future__ import annotations

from contextlib import ExitStack

from api.fastapi_app import create_app
from api.server.services import ApiServices
from control.service import ControlPlaneService
from fastapi.testclient import TestClient
from identity.auth import AuthService
from pydantic import JsonValue, TypeAdapter
from shared.identity import AuthScope

_JSON_OBJECT_ADAPTER = TypeAdapter(dict[str, JsonValue])


def test_workspace_audit_attributes_each_change_and_pages_in_order(
    isolated_services: ApiServices,
    client_stack: ExitStack,
) -> None:
    """Every workspace change names who made it, newest first, across a cursor."""
    workspace = ControlPlaneService(isolated_services.context).upsert_workspace("default")
    auth = AuthService(isolated_services.context)
    token, record = auth.create_token(
        "settings-owner",
        workspace_id=workspace.id,
        scopes=[AuthScope.Read.value, AuthScope.Write.value],
    )
    client = client_stack.enter_context(TestClient(create_app(isolated_services)))
    headers = _auth(token)

    for name in ("platform_team", "platform_ops", "platform_core"):
        renamed = client.patch("/api/v1/workspaces/current", headers=headers, json={"name": name})
        assert renamed.status_code == 200
        renamed_payload = _JSON_OBJECT_ADAPTER.validate_json(renamed.content)
        assert renamed_payload["id"] == workspace.id
        assert renamed_payload["name"] == name

    first_page = client.get("/api/v1/workspaces/audit?limit=2", headers=headers)
    assert first_page.status_code == 200
    first_payload = _JSON_OBJECT_ADAPTER.validate_json(first_page.content)
    assert _event_field_values(first_payload, "action") == [
        "workspace_renamed",
        "workspace_renamed",
    ]
    assert _event_field_values(first_payload, "actor_token_id") == [record.id, record.id]
    assert _event_field_values(first_payload, "actor_name") == [
        "settings-owner",
        "settings-owner",
    ]
    assert _event_field_values(first_payload, "new_value") == ["platform_core", "platform_ops"]

    cursor = first_payload["next"]
    assert isinstance(cursor, str) and cursor
    second_page = client.get(
        "/api/v1/workspaces/audit",
        headers=headers,
        params={"limit": 2, "cursor": cursor},
    )
    assert second_page.status_code == 200, second_page.text
    second_payload = _JSON_OBJECT_ADAPTER.validate_json(second_page.content)
    assert _event_field_values(second_payload, "new_value") == ["platform_team"]


def test_workspace_rename_requires_write_scope_and_valid_name(
    isolated_services: ApiServices,
    client_stack: ExitStack,
) -> None:
    workspace = ControlPlaneService(isolated_services.context).upsert_workspace("default")
    token, _ = AuthService(isolated_services.context).create_token(
        "reader",
        workspace_id=workspace.id,
        scopes=[AuthScope.Read.value],
    )
    client = client_stack.enter_context(TestClient(create_app(isolated_services)))

    denied = client.patch(
        "/api/v1/workspaces/current",
        headers=_auth(token),
        json={"name": "renamed"},
    )
    assert denied.status_code == 403

    writer_token, _ = AuthService(isolated_services.context).create_token(
        "writer",
        workspace_id=workspace.id,
        scopes=[AuthScope.Write.value],
    )
    invalid = client.patch(
        "/api/v1/workspaces/current",
        headers=_auth(writer_token),
        json={"name": "Not Valid"},
    )
    assert invalid.status_code == 422


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _event_field_values(payload: dict[str, JsonValue], field: str) -> list[JsonValue]:
    data = payload["data"]
    assert isinstance(data, list)
    return [item[field] for item in data if isinstance(item, dict)]
