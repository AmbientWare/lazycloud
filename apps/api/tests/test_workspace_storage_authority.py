from api.server.services import ApiServices
from control.service import ControlPlaneService
from fastapi.testclient import TestClient
from identity.auth import AuthService
from shared.identity import TokenKind, WorkspaceRecord


def test_workspace_storage_creation_preserves_credential_authority(
    api_runtime: tuple[ApiServices, TestClient], api_workspace: WorkspaceRecord
) -> None:
    services, client = api_runtime
    auth = AuthService(services.context)
    token, _ = auth.create_token(
        "storage-owner", kind=TokenKind.WorkspacePrimary, workspace_id=api_workspace.id
    )
    headers = {"Authorization": f"Bearer {token}"}
    assert client.get("/api/v1/workspaces", headers=headers).status_code == 200

    created = client.post("/api/v1/workspaces/create-storage", headers=headers)

    assert created.status_code == 201
    workspace = ControlPlaneService(services.context).get_workspace(api_workspace.id)
    assert workspace.storage.bucket
    assert created.json()["storage"]["bucket"] == workspace.storage.bucket
    assert client.get("/api/v1/workspaces", headers=headers).status_code == 200

    duplicate_token, _ = auth.create_token(
        "storage-owner-duplicate", kind=TokenKind.WorkspacePrimary, workspace_id=api_workspace.id
    )
    duplicate_headers = {"Authorization": f"Bearer {duplicate_token}"}
    assert client.get("/api/v1/workspaces", headers=duplicate_headers).status_code == 200
    duplicate = client.post("/api/v1/workspaces/create-storage", headers=duplicate_headers)

    assert duplicate.status_code == 400
    assert client.get("/api/v1/workspaces", headers=headers).status_code == 200
    assert client.get("/api/v1/workspaces", headers=duplicate_headers).status_code == 200
