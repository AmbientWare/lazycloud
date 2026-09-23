from uuid import uuid4

from api.fastapi_app import create_app
from api.server.services import ApiServices
from fastapi.testclient import TestClient
from identity.auth import AuthService
from shared.deployment_records import DeploymentSpec
from shared.http.deployment_plans import DeploymentPlanResponse, DeploymentPruneRequest
from shared.identity import AuthScope, TokenKind
from tests.workspaces import owned_workspace


def test_deployment_preview_is_read_only_and_pruning_requires_workspace_write_authority(
    isolated_services: ApiServices,
) -> None:
    services = isolated_services
    first = owned_workspace(services.control_plane_service, "prune-first")
    second = owned_workspace(services.control_plane_service, "prune-second")
    first_app = services.apps.create("shared_name", workspace=first.id)
    second_app = services.apps.create("shared_name", workspace=second.id)
    first_deployment = services.deployments.deploy(
        DeploymentSpec(name="old", handler="pkg:old", metadata={"app_id": first_app.id}),
        workspace=first.id,
    )
    second_deployment = services.deployments.deploy(
        DeploymentSpec(name="old", handler="pkg:old", metadata={"app_id": second_app.id}),
        workspace=second.id,
    )
    auth = AuthService(services.context)
    reader, _ = auth.create_token(
        "prune-reader",
        scopes=[AuthScope.Read.value],
        kind=TokenKind.Workspace,
        workspace_id=first.id,
    )
    writer, _ = auth.create_token(
        "prune-writer",
        scopes=[AuthScope.Read.value, AuthScope.Write.value],
        kind=TokenKind.Workspace,
        workspace_id=first.id,
    )
    reader_headers = {"Authorization": f"Bearer {reader}"}
    writer_headers = {"Authorization": f"Bearer {writer}"}
    with TestClient(create_app(services)) as client:
        preview = client.post(
            "/api/v1/deployment-plans",
            params={"workspace": first.id},
            headers=reader_headers,
            json={"app": first_app.name, "workloads": [], "prune": True},
        )
        assert preview.status_code == 200, preview.text
        plan = DeploymentPlanResponse.model_validate_json(preview.content)
        assert plan.app_id == first_app.id
        assert services.deployments.get(first_deployment.id).active
        request = DeploymentPruneRequest(
            app=plan.app,
            app_id=plan.app_id,
            snapshot=plan.snapshot,
            operation_id=uuid4(),
            workloads=[],
            deployment_ids=[],
        )
        denied = client.post(
            "/api/v1/deployment-prunes",
            params={"workspace": first.id},
            headers=reader_headers,
            json=request.model_dump(mode="json"),
        )
        assert denied.status_code == 403
        foreign = client.post(
            "/api/v1/deployment-prunes",
            params={"workspace": second.id},
            headers=writer_headers,
            json=request.model_dump(mode="json"),
        )
        assert foreign.status_code == 403
        removed = client.post(
            "/api/v1/deployment-prunes",
            params={"workspace": first.id},
            headers=writer_headers,
            json=request.model_dump(mode="json"),
        )
        assert removed.status_code == 200, removed.text
        assert services.deployments.list(app_id=first_app.id, workspace=first.id) == []
        assert services.deployments.get(second_deployment.id).active
