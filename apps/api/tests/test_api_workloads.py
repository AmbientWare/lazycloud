from contextlib import ExitStack

from api.fastapi_app import create_app
from api.server.services import ApiServices
from fastapi.testclient import TestClient
from shared.deployment_records import DeploymentSpec
from shared.deployments import DeploymentKind
from shared.http.apps import WorkloadPageResponse
from tests.service_fixtures import administrator_credential


def test_workload_deletion_removes_every_version_and_preserves_siblings(
    isolated_services: ApiServices,
    client_stack: ExitStack,
) -> None:
    versions = [
        isolated_services.deployments.deploy(
            DeploymentSpec(name="predict", handler=f"pkg:v{version}", metadata={"app": "demo"})
        )
        for version in range(1, 3)
    ]
    sibling = isolated_services.deployments.deploy(
        DeploymentSpec(name="sibling", handler="pkg:sibling", metadata={"app": "demo"})
    )
    raw_token, _ = administrator_credential(isolated_services, "workload-admin")
    client = client_stack.enter_context(TestClient(create_app(isolated_services)))
    headers = {"Authorization": f"Bearer {raw_token}"}
    path = f"/api/v1/apps/{versions[-1].app_id}/workloads"
    response = client.get(
        path, params={"name": "predict", "kind": DeploymentKind.Function.value}, headers=headers
    )
    assert response.status_code == 200
    page = WorkloadPageResponse.model_validate_json(response.content)
    assert page.data[0].deployment.id == versions[-1].id
    assert page.data[0].version_count == 2
    deleted = client.delete(f"{path}/predict", headers=headers)
    assert deleted.status_code == 204
    remaining = client.get(path, headers=headers)
    assert remaining.status_code == 200
    page = WorkloadPageResponse.model_validate_json(remaining.content)
    assert [item.deployment.id for item in page.data] == [sibling.id]
    assert client.delete(f"{path}/predict", headers=headers).status_code == 204
