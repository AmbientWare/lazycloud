from __future__ import annotations

from contextlib import ExitStack
from uuid import uuid4

from api.fastapi_app import create_app
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
from shared.identity import TokenKind
from tests.domain_fixtures import owned_workspace


def test_cross_workspace_resource_ids_are_not_found_from_another_workspace(
    isolated_services: ApiServices,
    client_stack: ExitStack,
) -> None:
    """A workspace B token gets typed not-found for workspace A's resource ids."""
    control = ControlPlaneService(isolated_services.context)
    owner = owned_workspace(control, "isolation-owner")
    intruder = owned_workspace(control, "isolation-intruder")
    owner_token = _workspace_token(isolated_services, owner.id, "owner-token")
    intruder_token = _workspace_token(isolated_services, intruder.id, "intruder-token")

    isolated_services.secrets.set("owned-secret", "owned-value", workspace=owner.id)
    task = isolated_services.tasks.create("owned-task", workspace_id=owner.id)
    container_id = str(uuid4())
    deployment_id = str(uuid4())
    with isolated_services.context.database.session() as session:
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
    _, owned_token_record = AuthService(isolated_services.context).create_token(
        "owned-secondary-token",
        kind=TokenKind.Workspace,
        workspace_id=owner.id,
    )

    client = client_stack.enter_context(TestClient(create_app(isolated_services)))
    owner_headers = _headers(owner_token)
    intruder_headers = _headers(intruder_token)

    # Owner sanity: every resource resolves inside its own workspace.
    assert client.get(f"/api/v1/tasks/{task.id}", headers=owner_headers).status_code == 200
    assert client.get("/api/v1/secrets/owned-secret", headers=owner_headers).status_code == 200
    assert (
        client.get(f"/api/v1/containers/{container_id}", headers=owner_headers).status_code == 200
    )

    # Cross-workspace get is a typed not-found.
    assert client.get(f"/api/v1/tasks/{task.id}", headers=intruder_headers).status_code == 404
    assert client.get("/api/v1/secrets/owned-secret", headers=intruder_headers).status_code == 404
    assert (
        client.get(f"/api/v1/containers/{container_id}", headers=intruder_headers).status_code
        == 404
    )

    # Cross-workspace delete is a typed not-found and does not remove the resource.
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
    assert AuthService(isolated_services.context).list_workspace_tokens(owner.id)

    # Cross-workspace listings never include the other tenant's resources.
    deployments = client.get("/api/v1/deployments", headers=intruder_headers)
    assert deployments.status_code == 200
    deployment_page = DeploymentListResponse.model_validate_json(deployments.content)
    assert deployment_id not in {item.id for item in deployment_page.data}
    tasks_page = client.get("/api/v1/tasks", headers=intruder_headers)
    assert tasks_page.status_code == 200
    task_page = TaskPageResponse.model_validate_json(tasks_page.content)
    assert task.id not in {item.id for item in task_page.data}


def _workspace_token(isolated_services: ApiServices, workspace_id: str, name: str) -> str:
    token, _record = AuthService(isolated_services.context).create_token(
        name,
        kind=TokenKind.Workspace,
        workspace_id=workspace_id,
    )
    return token


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}
