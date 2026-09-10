from __future__ import annotations

from api.server.services import ApiServices
from database.repositories.identity import WorkspaceRepository
from fastapi.testclient import TestClient
from tests.workspaces import administrator_credential


def test_authenticated_missing_workspace_requests_return_404_without_creating_rows(
    api_runtime: tuple[ApiServices, TestClient],
) -> None:
    services, client = api_runtime
    token, _ = administrator_credential(services.context, "workspace-lookup")
    headers = {"Authorization": f"Bearer {token}"}

    with services.database.session() as session:
        assert WorkspaceRepository(session).by_name("missing-workspace") is None

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
    with services.database.session() as session:
        assert WorkspaceRepository(session).by_name("missing-workspace") is None
