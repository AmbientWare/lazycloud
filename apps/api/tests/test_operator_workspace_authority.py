from __future__ import annotations

from contextlib import ExitStack

import pytest
from api.fastapi_app import create_app
from api.server.services import ApiServices
from control.service import ControlPlaneService
from fastapi.testclient import TestClient
from httpx2 import Response
from identity.auth import AuthService
from pydantic import JsonValue
from shared.identity import TokenKind
from tests.service_fixtures import owned_workspace


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
    isolated_services: ApiServices,
    client_stack: ExitStack,
    method: str,
    path: str,
    payload: dict[str, JsonValue] | None,
) -> None:
    control = ControlPlaneService(isolated_services.context)
    workspace_a = control.get_workspace("default")
    workspace_b = owned_workspace(control, "forged-target")
    workspace_token, _record = AuthService(isolated_services.context).create_token(
        "workspace-authority",
        kind=TokenKind.Workspace,
        workspace_id=workspace_a.id,
    )
    client = client_stack.enter_context(TestClient(create_app(isolated_services)))

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


def _successful(response: Response, status_code: int = 200) -> Response:
    assert response.status_code == status_code, response.text
    return response
