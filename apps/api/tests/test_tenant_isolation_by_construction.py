from __future__ import annotations

from uuid import uuid4

from api.server.services import ApiServices
from control.service import ControlPlaneService
from database.repositories.apps import DeploymentRepository
from database.repositories.orchestration import ContainerRepository
from fastapi.testclient import TestClient
from identity.auth import AuthService
from shared.containers import ContainerRecord
from shared.deployment_records import Deployment, DeploymentSpec
from shared.deployments import DeploymentKind
from shared.http.deployments import DeploymentListResponse
from shared.http.tasks import TaskPageResponse
from shared.http.volumes import GetOrCreateVolumeResponse, ListVolumesResponse
from shared.identity import TokenKind, TokenStatus
from tests.workspaces import owned_workspace


def test_cross_workspace_resource_ids_are_not_found_from_another_workspace(
    api_runtime: tuple[ApiServices, TestClient],
) -> None:
    services, client = api_runtime
    control = ControlPlaneService(services.context)
    owner = owned_workspace(control, "isolation-owner")
    intruder = owned_workspace(control, "isolation-intruder")
    owner_token = _workspace_token(services, owner.id, "owner-token")
    intruder_token = _workspace_token(services, intruder.id, "intruder-token")

    services.secrets.set("owned-secret", "owned-value", workspace=owner.id)
    task = services.tasks.create("owned-task", workspace_id=owner.id)
    container_id = str(uuid4())
    deployment_id = str(uuid4())
    with services.context.database.session() as session:
        ContainerRepository(session).upsert(
            ContainerRecord(
                id=container_id,
                name="owned-container",
                image="registry/image:1",
                command=["sleep"],
                workspace_id=owner.id,
            )
        )
        DeploymentRepository(session).upsert(
            Deployment(
                id=deployment_id,
                name="owned-deployment",
                kind=DeploymentKind.Endpoint,
                subdomain="owned-deployment-a1b2c3d4",
                spec=DeploymentSpec(name="owned-deployment", kind=DeploymentKind.Endpoint),
            ),
            workspace_id=owner.id,
        )
    _, owned_token_record = AuthService(services.context).create_token(
        "owned-secondary-token",
        kind=TokenKind.Workspace,
        workspace_id=owner.id,
    )

    owner_headers = _headers(owner_token)
    intruder_headers = _headers(intruder_token)
    created = client.post("/api/v1/volumes", json={"name": "owned-volume"}, headers=owner_headers)
    assert created.status_code == 201, created.text
    volume = GetOrCreateVolumeResponse.model_validate_json(created.content).volume
    assert volume is not None

    assert client.get(f"/api/v1/tasks/{task.id}", headers=owner_headers).status_code == 200
    assert client.get("/api/v1/secrets/owned-secret", headers=owner_headers).status_code == 200
    assert (
        client.get(f"/api/v1/containers/{container_id}", headers=owner_headers).status_code == 200
    )

    assert client.get(f"/api/v1/tasks/{task.id}", headers=intruder_headers).status_code == 404
    assert client.get("/api/v1/secrets/owned-secret", headers=intruder_headers).status_code == 404
    assert (
        client.get(f"/api/v1/containers/{container_id}", headers=intruder_headers).status_code
        == 404
    )

    assert (
        client.delete("/api/v1/secrets/owned-secret", headers=intruder_headers).status_code == 404
    )
    assert (
        client.delete(f"/api/v1/containers/{container_id}", headers=intruder_headers).status_code
        == 404
    )
    # 403 rather than 404: a token names an account, and this credential names a
    # workspace, so it is refused before anything is looked up by id at all.
    assert (
        client.post(
            f"/api/v1/tokens/{owned_token_record.id}/revoke",
            headers=intruder_headers,
        ).status_code
        == 403
    )
    assert (
        client.post(
            "/api/v1/volumes/owned-volume/delete",
            headers=intruder_headers,
        ).status_code
        == 404
    )
    assert client.get("/api/v1/secrets/owned-secret", headers=owner_headers).status_code == 200
    assert (
        client.get(f"/api/v1/containers/{container_id}", headers=owner_headers).status_code == 200
    )
    tokens = AuthService(services.context).list_workspace_tokens(owner.id)
    assert (
        next(token for token in tokens if token.id == owned_token_record.id).status
        is TokenStatus.Active
    )
    remaining = client.get("/api/v1/volumes", headers=owner_headers)
    assert remaining.status_code == 200, remaining.text
    assert [
        item.id for item in ListVolumesResponse.model_validate_json(remaining.content).volumes
    ] == [volume.id]

    deployments = client.get("/api/v1/deployments", headers=intruder_headers)
    assert deployments.status_code == 200
    deployment_page = DeploymentListResponse.model_validate_json(deployments.content)
    assert deployment_id not in {item.id for item in deployment_page.data}
    tasks_page = client.get("/api/v1/tasks", headers=intruder_headers)
    assert tasks_page.status_code == 200
    task_page = TaskPageResponse.model_validate_json(tasks_page.content)
    assert task.id not in {item.id for item in task_page.data}


def _workspace_token(services: ApiServices, workspace_id: str, name: str) -> str:
    token, _record = AuthService(services.context).create_token(
        name,
        kind=TokenKind.Workspace,
        workspace_id=workspace_id,
    )
    return token


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}
