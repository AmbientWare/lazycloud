from __future__ import annotations

from contextlib import ExitStack

from api.fastapi_app import create_app
from api.server.services import ApiServices
from database.repositories.identity import WorkspaceRepository
from fastapi.testclient import TestClient
from identity.auth import AuthService


def test_authenticated_missing_workspace_requests_return_404_without_creating_rows(
    isolated_services: ApiServices,
    client_stack: ExitStack,
) -> None:
    auth = AuthService(isolated_services.context)
    bootstrap = auth.bootstrap_admin_token(request_id="bootstrap:workspace-lookup-read-only")
    auth.mark_admin_token_published(
        request_id="bootstrap:workspace-lookup-read-only",
        recovery=False,
    )
    headers = {"Authorization": f"Bearer {bootstrap.token}"}
    client = client_stack.enter_context(TestClient(create_app(isolated_services)))

    with isolated_services.database.session() as session:
        before = [workspace.id for workspace in WorkspaceRepository(session).list()]

    workspace_response = client.get(
        "/api/v1/workspaces/missing-workspace",
        headers=headers,
    )
    resource_response = client.get(
        "/api/v1/units",
        params={"workspace": "missing-workspace"},
        headers=headers,
    )

    assert workspace_response.status_code == 404
    assert workspace_response.json()["detail"] == "workspace not found: missing-workspace"
    assert resource_response.status_code == 404
    assert resource_response.json()["detail"] == "workspace not found: missing-workspace"
    with isolated_services.database.session() as session:
        assert [workspace.id for workspace in WorkspaceRepository(session).list()] == before
