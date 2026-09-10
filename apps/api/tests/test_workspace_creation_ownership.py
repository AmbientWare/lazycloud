from __future__ import annotations

from api.server.services import ApiServices
from fastapi.testclient import TestClient
from identity.auth import AuthService
from shared.identity import PlatformRole

_DISPLAY_NAME = "creator"


def test_workspace_created_through_the_api_is_owned_and_reached_by_its_creator(
    api_runtime: tuple[ApiServices, TestClient],
) -> None:
    services, client = api_runtime
    creator = services.users.create(
        display_name=_DISPLAY_NAME,
        role=PlatformRole.Administrator,
    )
    raw_token, _record = AuthService(services.context).create_account_token(
        creator.id,
        "creator-cli",
    )
    headers = {"Authorization": f"Bearer {raw_token}"}

    created = client.post(
        "/api/v1/workspaces",
        json={"name": "creator-workspace"},
        headers=headers,
    )
    assert created.status_code == 201, created.text

    current = client.get("/api/v1/sessions/current", headers=headers)
    assert current.status_code == 200, current.text
    assert "creator-workspace" in {item["name"] for item in current.json()["workspaces"]}

    members = client.get("/api/v1/workspaces/creator-workspace/members", headers=headers)
    assert members.status_code == 200, members.text
    assert [(item["user_id"], item["role"]) for item in members.json()["data"]] == [
        (creator.id, "owner")
    ]
