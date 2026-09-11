from api.fastapi_app import create_app
from api.server.services import ApiServices
from fastapi.testclient import TestClient
from identity.auth import AuthService
from pydantic import JsonValue
from shared.deployment_records import DeploymentSpec
from shared.deployments import DeploymentKind
from shared.http.deployments import DeploymentListResponse
from shared.identity import AuthScope, TokenKind


def test_same_name_workload_history_is_scoped_by_kind_before_pagination(
    isolated_services: ApiServices,
) -> None:
    app = isolated_services.apps.create("workload_identity")
    metadata: dict[str, JsonValue] = {"app": app.name, "app_id": app.id}
    isolated_services.deployments.deploy(
        DeploymentSpec(name="hello", kind=DeploymentKind.Endpoint, metadata=metadata)
    )
    first = isolated_services.deployments.deploy(
        DeploymentSpec(name="hello", kind=DeploymentKind.Function, metadata=metadata)
    )
    second = isolated_services.deployments.deploy(
        DeploymentSpec(name="hello", kind=DeploymentKind.Function, metadata=metadata)
    )
    token, _ = AuthService(isolated_services.context).create_token(
        "workload-reader",
        scopes=[AuthScope.Read.value],
        kind=TokenKind.Workspace,
        workspace_id=app.workspace_id,
    )
    params = {"app_id": app.id, "name": "hello", "kind": "function", "limit": "1"}
    with TestClient(create_app(isolated_services)) as client:
        client.headers["Authorization"] = f"Bearer {token}"
        response = client.get("/api/v1/deployments", params=params)
        assert response.status_code == 200
        page = DeploymentListResponse.model_validate_json(response.content)
        assert [row.id for row in page.data] == [second.id]
        assert page.next

        response = client.get("/api/v1/deployments", params={**params, "cursor": page.next})
        assert response.status_code == 200
        page = DeploymentListResponse.model_validate_json(response.content)
        assert [row.id for row in page.data] == [first.id]
        assert not page.next

        response = client.get("/api/v1/deployments", params={**params, "latest": "true"})
        assert response.status_code == 200
        page = DeploymentListResponse.model_validate_json(response.content)
        assert [row.id for row in page.data] == [second.id]
