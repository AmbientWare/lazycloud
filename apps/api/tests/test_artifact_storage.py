from __future__ import annotations

from datetime import timedelta
from uuid import uuid4

from api.server.services import ApiServices
from database.repositories.execution import TaskRepository
from fastapi.testclient import TestClient
from identity.auth import AuthService
from shared.bytes_transport import encode_bytes
from shared.http.artifacts import ArtifactListResponse, ArtifactSaveResponse
from shared.identity import TokenKind, WorkspaceRecord
from shared.tasks import Task
from shared.timestamps import utc_now
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
    body = {
        "task_id": task_id,
        "filename": "résumé.txt",
        "content_type": "text/plain",
        "value_base64": encode_bytes(b"report"),
    }
    before_save = utc_now()
    saved_response = client.post(f"{base}/save", json=body)
    assert saved_response.status_code == 200, saved_response.text
    saved = ArtifactSaveResponse.model_validate(saved_response.json())
    second_saved = ArtifactSaveResponse.model_validate(
        client.post(f"{base}/save", json=body).json()
    )
    assert before_save + timedelta(days=1) <= saved.expires_at <= utc_now() + timedelta(days=1)
    assert second_saved.expires_at > before_save
    for override in (None, 0, True, 3600):
        response = client.post(f"{base}/save", json={**body, "retention_seconds": override})
        assert response.status_code == 422, response.text
    first = ArtifactListResponse.model_validate(client.get(base, params={"limit": 1}).json())
    second = ArtifactListResponse.model_validate(
        client.get(base, params={"limit": 1, "cursor": first.next}).json()
    )
    assert {first.data[0].id, second.data[0].id} == {saved.id, second_saved.id}
    assert not second.next
    assert client.get(base, params={"workspace": other.id}).status_code == 403
    other_token, _ = AuthService(services.context).create_token(
        "neighbor", kind=TokenKind.Workspace, workspace_id=other.id
    )
    content_params = {"id": saved.id, "task_id": task_id, "filename": "résumé.txt"}
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
            f"{base}/{saved.id}",
            params={"workspace": other.id},
            headers={"Authorization": f"Bearer {other_token}"},
        ).status_code
        == 204
    )
    with services.context.database.session() as session:
        TaskRepository(session).records.delete(task_id, workspace_id=workspace.id)
    assert client.get(f"{base}/content", params=content_params).content == b"report"
    assert client.delete(f"{base}/{saved.id}").status_code == 204
    assert client.get(f"{base}/content", params=content_params).status_code == 404
    assert [
        row.id for row in ArtifactListResponse.model_validate(client.get(base).json()).data
    ] == [second_saved.id]
