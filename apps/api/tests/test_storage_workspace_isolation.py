from __future__ import annotations

from contextlib import ExitStack

import pytest
from api.fastapi_app import create_app
from api.server.services import ApiServices
from control.service import ControlPlaneService
from fastapi.testclient import TestClient
from identity.auth import AuthService
from shared.deployment_records import DeploymentSpec, VolumeMount
from shared.deployments import DeploymentKind
from shared.identity import TokenKind
from shared.mounts import MountAuthMode
from storage.service import ObjectStorage
from tests.fakes import FakeObjectClient
from tests.service_fixtures import owned_workspace


def test_workspace_object_cleanup_preserves_external_bucket_data(
    isolated_services: ApiServices,
) -> None:
    workspace = owned_workspace(ControlPlaneService(isolated_services.context), "cleanup-tenant")
    object_client = FakeObjectClient()
    storage = ObjectStorage(
        isolated_services.context,
        object_client=object_client,
        default_bucket="owned",
    )
    storage.put_bytes_for_workspace(
        workspace_id=workspace.id,
        bucket="owned",
        key="artifacts/task/result.txt",
        data=b"owned-data",
    )
    object_client.put_bytes(
        "customer/preserved.txt",
        b"external-data",
        bucket="external",
    )

    assert storage.delete_workspace_objects(workspace.id) == 1
    assert ("owned", "artifacts/task/result.txt") not in object_client.objects
    assert object_client.objects[("external", "customer/preserved.txt")] == b"external-data"


def test_secret_relationships_decode_persisted_cloud_bucket_credentials(
    isolated_services: ApiServices,
    request: pytest.FixtureRequest,
) -> None:
    control = ControlPlaneService(isolated_services.context)
    workspace = owned_workspace(control, "relationship-tenant")
    token = _workspace_token(isolated_services, workspace.id, "relationship-token")
    isolated_services.secrets.set("ACCESS_KEY", "access", workspace=workspace.id)
    isolated_services.secrets.set("SECRET_KEY", "secret", workspace=workspace.id)
    deployment = isolated_services.deployments.deploy(
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
    client_stack = ExitStack()
    request.addfinalizer(client_stack.close)
    client = client_stack.enter_context(TestClient(create_app(isolated_services)))

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
