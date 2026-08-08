from __future__ import annotations

from contextlib import ExitStack

from api.fastapi_app import create_app
from api.server.services import ApiServices
from fastapi.testclient import TestClient
from shared.identity import PlatformRole

_USERNAME = "creator"
_PASSWORD = "creator-password"


def test_workspace_created_through_the_api_is_owned_and_reached_by_its_creator(
    isolated_services: ApiServices,
    client_stack: ExitStack,
) -> None:
    """A workspace nobody owns is a workspace nobody reaches.

    A person reaches a workspace only through a membership row, and the account a
    workspace's compute and domains resolve through is its owner row. Creation writes
    both or it hands back a workspace the creator is refused from and nothing can
    resolve an account for.
    """
    isolated_services.users.create(
        username=_USERNAME,
        password=_PASSWORD,
        role=PlatformRole.Administrator,
    )
    client = client_stack.enter_context(TestClient(create_app(isolated_services)))
    session = client.post(
        "/api/v1/sessions",
        json={"username": _USERNAME, "password": _PASSWORD},
    )
    assert session.status_code == 201, session.text
    headers = {"Authorization": f"Bearer {session.json()['token']}"}

    created = client.post(
        "/api/v1/workspaces",
        # Naming storage is the branch that attaches a bucket the caller already has,
        # which is what keeps this case about ownership rather than about provisioning
        # one against an object store this suite does not run.
        json={
            "name": "creator-workspace",
            "storage": {"backend": "local", "bucket": "creator-workspace", "prefix": ""},
        },
        headers=headers,
    )
    assert created.status_code == 201, created.text

    current = client.get("/api/v1/sessions/current", headers=headers)
    assert current.status_code == 200, current.text
    assert "creator-workspace" in {item["name"] for item in current.json()["workspaces"]}

    members = client.get("/api/v1/workspaces/creator-workspace/members", headers=headers)
    assert members.status_code == 200, members.text
    assert [(item["username"], item["role"]) for item in members.json()["data"]] == [
        (_USERNAME, "owner")
    ]
