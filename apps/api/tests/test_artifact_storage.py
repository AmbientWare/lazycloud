from __future__ import annotations

from uuid import uuid4

from api.server.services import ApiServices
from database.repositories.execution import TaskRepository
from fastapi.testclient import TestClient
from identity.auth import AuthService
from shared.artifacts import ArtifactRetentionSource
from shared.bytes_transport import encode_bytes
from shared.http.artifacts import ArtifactListResponse, ArtifactSaveResponse
from shared.identity import TokenKind, WorkspaceRecord
from shared.tasks import Task
from tests.workspaces import owned_workspace


def test_artifact_retention_and_access_survive_task_deletion_without_crossing_workspaces(
    api_runtime: tuple[ApiServices, TestClient],
    api_workspace: WorkspaceRecord,
    api_client: TestClient,
) -> None:
    services, _ = api_runtime
    workspace = api_workspace
    other = owned_workspace(services.control_plane_service, f"artifact-neighbor-{workspace.id}")
    token, _ = AuthService(services.context).create_token(
        "artifact-owner", kind=TokenKind.Workspace, workspace_id=workspace.id
    )
    task_id = str(uuid4())
    with services.context.database.session() as session:
        TaskRepository(session).upsert(Task(id=task_id, name="produce", workspace_id=workspace.id))
    client = api_client
    client.headers["Authorization"] = f"Bearer {token}"
    base = "/api/v1/artifacts"
    assert client.put(f"{base}/retention", json={"retention_seconds": 3600}).status_code == 200
    body = {
        "task_id": task_id,
        "filename": "résumé.txt",
        "content_type": "text/plain",
        "value_base64": encode_bytes(b"report"),
    }
    inherited_response = client.post(f"{base}/save", json=body)
    assert inherited_response.status_code == 200, inherited_response.text
    inherited = ArtifactSaveResponse.model_validate(inherited_response.json())
    kept = ArtifactSaveResponse.model_validate(
        client.post(f"{base}/save", json={**body, "retention_seconds": None}).json()
    )
    assert inherited.expires_at is not None
    assert inherited.retention_source is ArtifactRetentionSource.Workspace
    assert kept.expires_at is None
    assert kept.retention_source is ArtifactRetentionSource.Explicit
    assert client.post(f"{base}/save", json={**body, "retention_seconds": 0}).status_code == 422
    assert client.post(f"{base}/save", json={**body, "retention_seconds": True}).status_code == 422
    first = ArtifactListResponse.model_validate(client.get(base, params={"limit": 1}).json())
    second = ArtifactListResponse.model_validate(
        client.get(base, params={"limit": 1, "cursor": first.next}).json()
    )
    assert {first.data[0].id, second.data[0].id} == {inherited.id, kept.id}
    assert not second.next
    assert client.get(base, params={"workspace": other.id}).status_code == 403
    other_token, _ = AuthService(services.context).create_token(
        "neighbor", kind=TokenKind.Workspace, workspace_id=other.id
    )
    content_params = {"id": inherited.id, "task_id": task_id, "filename": "résumé.txt"}
    assert (
        client.get(
            f"{base}/content",
            params={**content_params, "workspace": other.id},
            headers={"Authorization": f"Bearer {other_token}"},
        ).status_code
        == 404
    )
    assert (
        client.delete(
            f"{base}/{inherited.id}",
            params={"workspace": other.id},
            headers={"Authorization": f"Bearer {other_token}"},
        ).status_code
        == 204
    )
    with services.context.database.session() as session:
        TaskRepository(session).records.delete(task_id, workspace_id=workspace.id)
    assert client.get(f"{base}/content", params=content_params).content == b"report"
    applied = client.post(
        f"{base}/retention/apply", json={"ids": [inherited.id, kept.id], "retention_seconds": 7200}
    )
    assert applied.status_code == 200, applied.text
    assert [row["id"] for row in applied.json()["data"]] == [inherited.id]
    assert client.delete(f"{base}/{inherited.id}").status_code == 204
    assert client.get(f"{base}/content", params=content_params).status_code == 404
    assert [
        row.id for row in ArtifactListResponse.model_validate(client.get(base).json()).data
    ] == [kept.id]
