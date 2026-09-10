from __future__ import annotations

from contextlib import ExitStack
from typing import Protocol

from api.fastapi_app import create_app
from api.server.services import ApiServices
from fastapi.testclient import TestClient
from pydantic import JsonValue, TypeAdapter
from shared.deployment_records import DeploymentSpec
from shared.deployments import DeploymentKind
from shared.http.client_manifests import (
    ClientContract,
    ClientOperation,
    ClientOperationName,
    ClientParameter,
)
from tests.workspaces import administrator_credential

_JSON_VALUE_ADAPTER: TypeAdapter[JsonValue] = TypeAdapter(JsonValue)


class _HttpResponse(Protocol):
    @property
    def content(self) -> bytes: ...


def test_deployment_manifest_route_serves_invoke_schema(
    isolated_services: ApiServices,
) -> None:
    with ExitStack() as client_stack:
        deployment = isolated_services.deployments.deploy(
            DeploymentSpec(
                name="square",
                kind=DeploymentKind.Function,
                handler="pkg:square",
                metadata={
                    "app": "demo",
                    "inputs": {"fields": {"value": {"type": "integer"}}},
                    "outputs": {"fields": {"return": {"type": "integer"}}},
                },
                client_contract=ClientContract(
                    operation=ClientOperation(
                        name=ClientOperationName.Remote,
                        parameters=[ClientParameter(name="value", json_schema={"type": "integer"})],
                        return_schema={"type": "integer"},
                    )
                ),
            )
        )
        pod = isolated_services.deployments.deploy(
            DeploymentSpec(
                name="pod-worker",
                kind=DeploymentKind.Pod,
                metadata={"app": "demo"},
                command=["python", "-m", "http.server", "8080"],
                ports={"http": 8080},
            )
        )

        raw_token, _ = administrator_credential(isolated_services.context, "manifest-admin")
        client = client_stack.enter_context(TestClient(create_app(isolated_services)))
        headers = {"Authorization": f"Bearer {raw_token}"}

        response = client.get(
            f"/api/v1/deployments/{deployment.id}/manifest",
            headers=headers,
            params={"external_url": "https://ui.example"},
        )
        assert response.status_code == 200
        manifest = _response_json(response)
        assert _json_path(manifest, "app") == "demo"
        assert _json_path(manifest, "name") == "square"
        assert _json_path(manifest, "kind") == "function"
        assert _json_path(manifest, "deployment_id") == deployment.id
        invoke_url = _json_path(manifest, "invoke_url")
        assert isinstance(invoke_url, str)
        assert invoke_url == f"https://{deployment.subdomain}.ui.example"
        assert _json_path(manifest, "inputs", "fields", "value", "type") == "integer"
        assert _json_path(manifest, "client_contract", "operation", "name") == "remote"
        assert (
            _json_path(manifest, "client_contract", "operation", "parameters", 0, "name") == "value"
        )
        assert _json_path(
            manifest,
            "client_contract",
            "operation",
            "return_schema",
        ) == {"type": "integer"}

        not_invokable = client.get(
            f"/api/v1/deployments/{pod.id}/manifest",
            headers=headers,
        )
        assert not_invokable.status_code == 400

        missing = client.get(
            "/api/v1/deployments/does-not-exist/manifest",
            headers=headers,
        )
        assert missing.status_code == 404


def _response_json(response: _HttpResponse) -> JsonValue:
    return _JSON_VALUE_ADAPTER.validate_json(response.content)


def _json_path(value: JsonValue, *path: str | int) -> JsonValue:
    current = value
    for segment in path:
        if isinstance(segment, str):
            assert isinstance(current, dict)
            current = current[segment]
        else:
            assert isinstance(current, list)
            current = current[segment]
    return current
