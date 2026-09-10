from __future__ import annotations

import pytest
from api.server.services import ApiServices
from control.service import ControlPlaneService
from fastapi.testclient import TestClient
from identity.auth import AuthService
from pydantic import JsonValue
from shared.identity import TokenKind, WorkspaceRecord
from tests.workspaces import owned_workspace


@pytest.mark.parametrize(
    ("method", "path", "payload"),
    [
        (
            "POST",
            "/api/v1/containers",
            {"name": "forged", "image": "python:3.12", "command": ["true"]},
        ),
        ("POST", "/api/v1/units", {"name": "forged", "provider": "local"}),
        ("POST", "/api/v1/machines", {"pool": "forged", "provider": "local"}),
        ("GET", "/api/v1/cron-jobs", None),
        ("DELETE", "/api/v1/cron-jobs/forged", None),
        ("GET", "/api/v1/secrets/full", None),
        ("POST", "/api/v1/secrets", {"name": "FORGED", "value": "denied"}),
    ],
)
def test_workspace_token_cannot_forge_operator_workspace_override(
    api_runtime: tuple[ApiServices, TestClient],
    api_workspace: WorkspaceRecord,
    method: str,
    path: str,
    payload: dict[str, JsonValue] | None,
) -> None:
    services, client = api_runtime
    control = ControlPlaneService(services.context)
    workspace_a = api_workspace
    workspace_b = owned_workspace(control, f"forged-target-{api_workspace.id}")
    workspace_token, _record = AuthService(services.context).create_token(
        "workspace-authority",
        kind=TokenKind.Workspace,
        workspace_id=workspace_a.id,
    )

    response = client.request(
        method,
        path,
        params={"workspace": workspace_b.name},
        headers=_auth(workspace_token),
        json=payload,
    )

    assert response.status_code == 403, response.text


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}
