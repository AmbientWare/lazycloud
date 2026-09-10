from __future__ import annotations

from api.server.services import ApiServices
from fastapi.testclient import TestClient
from identity.auth import AuthService
from shared.deployment_records import DeploymentSpec, VolumeMount
from shared.deployments import DeploymentKind
from shared.identity import TokenKind, WorkspaceRecord
from shared.mounts import MountAuthMode


def test_secret_relationships_decode_persisted_cloud_bucket_credentials(
    api_runtime: tuple[ApiServices, TestClient],
    api_workspace: WorkspaceRecord,
) -> None:
    services, client = api_runtime
    workspace = api_workspace
    token = _workspace_token(services, workspace.id, "relationship-token")
    services.secrets.set("ACCESS_KEY", "access", workspace=workspace.id)
    services.secrets.set("SECRET_KEY", "secret", workspace=workspace.id)
    deployment = services.deployments.deploy(
        DeploymentSpec(
            name="cloud-bucket-reader",
            kind=DeploymentKind.Function,
            handler="pkg:read",
            volumes=[
                VolumeMount(
                    name="customer-bucket",
                    mount_path="/mnt/customer",
                    config={
                        "auth_mode": MountAuthMode.SecretReferences.value,
                        "access_key": "ACCESS_KEY",
                        "secret_key": "SECRET_KEY",
                        "bucket_name": "customer-data",
                    },
                )
            ],
        ),
        workspace=workspace.id,
    )

    response = client.get(
        "/api/v1/secrets/ACCESS_KEY",
        headers=_headers(token),
    )

    assert response.status_code == 200, response.text
    assert response.json()["secret"]["workloads"] == [
        {
            "app_id": deployment.app_id,
            "app_name": "cloud_bucket_reader",
            "name": deployment.name,
            "kind": DeploymentKind.Function.value,
            "versions": [deployment.version],
            "active_versions": [deployment.version],
        }
    ]


def _workspace_token(isolated_services: ApiServices, workspace_id: str, name: str) -> str:
    token, _record = AuthService(isolated_services.context).create_token(
        name,
        kind=TokenKind.Workspace,
        workspace_id=workspace_id,
    )
    return token


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}
