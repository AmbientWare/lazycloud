from __future__ import annotations

import json
import socket
from collections.abc import AsyncIterator, Sequence
from contextlib import ExitStack
from urllib.parse import urlsplit

import pytest
from api.fastapi_app import create_app
from api.server.services import ApiServices
from control.service import ControlPlaneService, StubRecord
from execution.endpoints.dispatch import AsyncEndpointResponseStream, EndpointDispatchTarget
from execution.endpoints.service import EndpointIngressDispatchSession
from fastapi.testclient import TestClient
from identity.auth import AuthService
from operations.management import ManagementService
from pydantic import JsonValue, TypeAdapter
from shared.deployment_records import Deployment, DeploymentSpec
from shared.deployments import DeploymentKind
from shared.http.endpoints import (
    EndpointForwardRequest,
    EndpointForwardResponse,
    StartEndpointServeRequest,
    StartEndpointServeResponse,
)
from shared.http.functions import (
    FunctionClaimRequest,
    FunctionClaimResponse,
    FunctionInvokeBody,
    FunctionInvokeResponse,
    FunctionMonitorRequest,
    FunctionMonitorResponse,
    FunctionSetResultBody,
    FunctionSetResultResponse,
)
from tests.url_constants import TEST_URL
from tests.workspaces import owned_workspace

BASE_URL = TEST_URL
_JSON_OBJECT_ADAPTER = TypeAdapter(dict[str, JsonValue])


class RecordingFunctionService:
    def __init__(self) -> None:
        self.requests: list[FunctionInvokeBody] = []

    def function_invoke(self, request: FunctionInvokeBody) -> FunctionInvokeResponse:
        self.requests.append(request)
        return FunctionInvokeResponse.from_result(task_id=f"fn-{len(self.requests)}")

    async def function_invoke_stream(
        self,
        initial: FunctionInvokeResponse,
        *,
        headless: bool = False,
        keepalive_interval_seconds: float = 5.0,
    ) -> AsyncIterator[FunctionInvokeResponse]:
        _ = headless, keepalive_interval_seconds
        yield initial

    def function_set_result(
        self,
        request: FunctionSetResultBody,
    ) -> FunctionSetResultResponse:
        raise AssertionError(f"unexpected function_set_result call: {request}")

    def function_monitor(self, request: FunctionMonitorRequest) -> FunctionMonitorResponse:
        raise AssertionError(f"unexpected function_monitor call: {request}")

    # The rest of the protocol, refusing rather than answering. These routes
    # never reach admission or claiming, and a fake that returned a plausible
    # value would let a route that started calling one of them keep passing.

    def unclaimed_task_counts(self, stub_ids: Sequence[str]) -> dict[str, int]:
        raise AssertionError(f"unexpected unclaimed_task_counts call: {stub_ids}")

    def start_function_container(self, stub_id: str) -> bool:
        raise AssertionError(f"unexpected start_function_container call: {stub_id}")

    def containers_holding_work(self, container_ids: Sequence[str]) -> set[str]:
        raise AssertionError(f"unexpected containers_holding_work call: {container_ids}")

    def fail_unclaimed_tasks(
        self,
        stub_id: str,
        *,
        error: str,
        limit: int = 100,
    ) -> int:
        raise AssertionError(
            f"unexpected fail_unclaimed_tasks call: {stub_id}, {error}, limit={limit}"
        )

    def function_claim(self, request: FunctionClaimRequest) -> FunctionClaimResponse:
        raise AssertionError(f"unexpected function_claim call: {request}")


class RecordingEndpointService:
    def __init__(self) -> None:
        self.forward_requests: list[EndpointForwardRequest] = []
        self.serve_requests: list[StartEndpointServeRequest] = []

    async def forward_endpoint_request(
        self,
        request: EndpointForwardRequest,
    ) -> EndpointForwardResponse:
        self.forward_requests.append(request)
        body = json.dumps(
            {
                "stub_id": request.stub_id,
                "method": request.method,
                "path": request.path,
                "query_params": request.query_params,
                "headers": request.headers,
                "body": request.body.decode("utf-8"),
            }
        ).encode("utf-8")
        return EndpointForwardResponse(
            status_code=202,
            headers={
                "content-type": ["application/json"],
                "x-forwarded-stub": [request.stub_id],
                "x-forwarded-path": [request.path],
            },
            body=body,
        )

    async def forward_endpoint_health(
        self,
        request: EndpointForwardRequest,
    ) -> EndpointForwardResponse:
        # These routes forward invocations, never probes. Answering one would
        # let a route that started asking for readiness pass without saying so.
        raise AssertionError(f"unexpected forward_endpoint_health call: {request}")

    def start_endpoint_serve(
        self,
        request: StartEndpointServeRequest,
    ) -> StartEndpointServeResponse:
        self.serve_requests.append(request)
        return StartEndpointServeResponse(
            container_id=f"endpoint-{len(self.serve_requests)}",
        )

    async def prepare_asgi_websocket(
        self,
        request: EndpointForwardRequest,
    ) -> EndpointIngressDispatchSession:
        raise AssertionError(f"unexpected prepare_asgi_websocket call: {request}")

    async def prepare_asgi_http(
        self,
        request: EndpointForwardRequest,
    ) -> EndpointIngressDispatchSession:
        return EndpointIngressDispatchSession(
            task_id=f"asgi-{len(self.forward_requests) + 1}",
            stub_id=request.stub_id,
            workspace_id="workspace",
            target=EndpointDispatchTarget(container_id="container", address="backend"),
            headers=request.headers,
            wait_timeout_seconds=1,
        )

    async def open_asgi_http_stream(
        self,
        session: EndpointIngressDispatchSession,
        request: EndpointForwardRequest,
    ) -> AsyncEndpointResponseStream:
        _ = session
        return StaticEndpointResponseStream(await self.forward_endpoint_request(request))

    async def finish_asgi_http(
        self,
        task_id: str,
        *,
        status_code: int | None = None,
        body_size_bytes: int = 0,
        cancelled: bool = False,
        error: str | None = None,
    ) -> None:
        _ = task_id, status_code, body_size_bytes, cancelled, error

    async def open_asgi_websocket_socket(
        self,
        session: EndpointIngressDispatchSession,
    ) -> socket.socket | None:
        _ = session
        return None

    async def heartbeat_asgi_websocket(self, task_id: str) -> None:
        raise AssertionError(f"unexpected heartbeat_asgi_websocket call: {task_id}")

    async def finish_asgi_websocket(
        self,
        task_id: str,
        *,
        cancelled: bool = False,
        error: str | None = None,
    ) -> None:
        raise AssertionError(
            "unexpected finish_asgi_websocket call: "
            f"{task_id}, cancelled={cancelled}, error={error}"
        )


class StaticEndpointResponseStream:
    def __init__(self, response: EndpointForwardResponse) -> None:
        self.status_code = response.status_code
        self.headers = response.headers
        self._body = response.body

    async def iter_chunks(self) -> AsyncIterator[bytes]:
        yield self._body

    async def close(self) -> None:
        return


def test_unversioned_invoke_rejects_stopped_latest_without_fallback(
    isolated_services: ApiServices,
) -> None:
    with ExitStack() as client_stack:
        _v1_deployment, v1_stub = _deploy(isolated_services, "roll", DeploymentKind.Function)
        v2_deployment, _v2_stub = _deploy(isolated_services, "roll", DeploymentKind.Function)
        ManagementService(isolated_services).set_deployment_active(
            "default", v2_deployment.id, active=False
        )
        service = RecordingFunctionService()
        client = client_stack.enter_context(
            TestClient(create_app(isolated_services, function_service=service))
        )
        headers = _auth_headers(isolated_services)

        latest_response = client.post(
            "/api/v1/functions/roll/latest", headers=headers, json={"args": [1]}
        )
        versioned_response = client.post(
            "/api/v1/functions/roll/v1", headers=headers, json={"args": [1]}
        )

        assert latest_response.status_code == 400
        assert "not active" in latest_response.json()["detail"]
        assert versioned_response.status_code == 200
        assert [request.stub_id for request in service.requests] == [v1_stub.id]


def test_private_function_deployed_routes_use_token_workspace(
    isolated_services: ApiServices,
) -> None:
    with ExitStack() as client_stack:
        deployment, stub = _deploy(
            isolated_services,
            "owner-private-fn",
            DeploymentKind.Function,
            workspace="route-owner",
        )
        _public_deployment, public_stub = _deploy(
            isolated_services,
            "owner-public-fn",
            DeploymentKind.Function,
            route="/owner-public-fn",
            workspace="public-owner",
            public=True,
        )
        service = RecordingFunctionService()
        client = client_stack.enter_context(
            TestClient(create_app(isolated_services, function_service=service))
        )
        owner_headers = _auth_headers(isolated_services, workspace="route-owner")
        other_headers = _auth_headers(isolated_services, workspace="route-other")

        private_paths = [
            f"/api/v1/functions/id/{stub.id}",
            f"/api/v1/functions/{deployment.name}/latest",
            f"/api/v1/functions/{deployment.name}/v{deployment.version}",
        ]
        for path in private_paths:
            response = client.post(path, headers=other_headers, json={"value": "blocked"})
            assert response.status_code == 404

        private_public_response = client.post(
            f"/api/v1/functions/public/{stub.id}",
            json={"value": "blocked"},
        )
        owner_response = client.post(
            f"/api/v1/functions/{deployment.name}/latest",
            headers=owner_headers,
            json={"value": "owner"},
        )
        public_path = f"/api/v1/functions/public/{public_stub.id}"
        public_response = client.post(public_path, json={"value": "public"})

        assert private_public_response.status_code == 404
        assert owner_response.status_code == 200
        assert public_response.status_code == 200
        assert [request.stub_id for request in service.requests] == [stub.id, public_stub.id]


def test_endpoint_version_routes_follow_deployment_lifecycle(
    isolated_services: ApiServices,
) -> None:
    with ExitStack() as client_stack:
        v1_deployment, v1_stub = _deploy(
            isolated_services,
            "versioned-endpoint",
            DeploymentKind.Endpoint,
        )
        v2_deployment, v2_stub = _deploy(
            isolated_services,
            "versioned-endpoint",
            DeploymentKind.Endpoint,
        )
        management = ManagementService(isolated_services)
        service = RecordingEndpointService()
        client = client_stack.enter_context(
            TestClient(create_app(isolated_services, endpoint_service=service))
        )
        headers = _auth_headers(isolated_services)
        latest_path = "/api/v1/endpoints/versioned-endpoint/latest"
        v1_path = f"/api/v1/endpoints/versioned-endpoint/v{v1_deployment.version}"
        v2_path = f"/api/v1/endpoints/versioned-endpoint/v{v2_deployment.version}"

        management.set_deployment_active("default", v2_deployment.id, active=False)
        stopped_latest = client.post(latest_path, headers=headers, json={})
        stopped_v2 = client.post(v2_path, headers=headers, json={})
        active_v1 = client.post(v1_path, headers=headers, json={})

        assert stopped_latest.status_code == 400
        assert "not active" in stopped_latest.json()["detail"]
        assert stopped_v2.status_code == 400
        assert active_v1.status_code == 202
        assert service.forward_requests[-1].stub_id == v1_stub.id

        management.set_deployment_active("default", v2_deployment.id, active=True)
        restarted_latest = client.post(latest_path, headers=headers, json={})

        assert restarted_latest.status_code == 202
        assert service.forward_requests[-1].stub_id == v2_stub.id

        management.delete_deployment("default", v1_deployment.id)
        deleted_v1 = client.post(v1_path, headers=headers, json={})

        assert deleted_v1.status_code == 404


def test_endpoint_host_routing_preserves_numeric_deployment_suffixes(
    isolated_services: ApiServices,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with ExitStack() as client_stack:
        monkeypatch.setattr(isolated_services.gateway_settings, "public_http_url", BASE_URL)
        _deploy(isolated_services, "analytics", DeploymentKind.Endpoint)
        deployment, stub = _deploy(isolated_services, "analytics-2026", DeploymentKind.Endpoint)
        service = RecordingEndpointService()
        client = client_stack.enter_context(
            TestClient(create_app(isolated_services, endpoint_service=service))
        )
        headers = _auth_headers(isolated_services)

        latest_response = client.post(
            "/",
            headers=headers | {"host": f"{deployment.subdomain}.{_base_host(BASE_URL)}"},
            json={"value": "numeric-suffix"},
        )

        assert latest_response.status_code == 202
        assert latest_response.json()["stub_id"] == stub.id
        assert [request.stub_id for request in service.forward_requests] == [stub.id]


def test_private_endpoint_and_asgi_id_routes_use_token_workspace(
    isolated_services: ApiServices,
) -> None:
    with ExitStack() as client_stack:
        _, endpoint_stub = _deploy(
            isolated_services,
            "owner-endpoint",
            DeploymentKind.Endpoint,
            workspace="endpoint-owner",
        )
        _, asgi_stub = _deploy(
            isolated_services,
            "owner-asgi",
            DeploymentKind.Asgi,
            workspace="endpoint-owner",
        )
        service = RecordingEndpointService()
        client = client_stack.enter_context(
            TestClient(create_app(isolated_services, endpoint_service=service))
        )
        other_headers = _auth_headers(isolated_services, workspace="endpoint-other")

        endpoint_response = client.post(
            f"/api/v1/endpoints/id/{endpoint_stub.id}",
            headers=other_headers,
            json={"value": "blocked"},
        )
        asgi_response = client.get(
            f"/api/v1/asgi/id/{asgi_stub.id}/health",
            headers=other_headers,
        )

        assert endpoint_response.status_code == 404
        assert asgi_response.status_code == 404
        assert service.forward_requests == []


def test_generated_asgi_urls_forward_subpaths_and_warmup(
    isolated_services: ApiServices,
) -> None:
    with ExitStack() as client_stack:
        deployment, stub = _deploy(isolated_services, "web", DeploymentKind.Asgi)
        _public_deployment, public_stub = _deploy(
            isolated_services,
            "public-web",
            DeploymentKind.Asgi,
            route="/public-web",
            public=True,
        )
        service = RecordingEndpointService()
        client = client_stack.enter_context(
            TestClient(create_app(isolated_services, endpoint_service=service))
        )
        headers = _auth_headers(isolated_services)

        id_path = f"/api/v1/asgi/id/{stub.id}"
        deployment_path = f"/api/v1/asgi/{deployment.name}/v{deployment.version}"
        public_path = f"/api/v1/asgi/public/{public_stub.id}"
        latest_path = f"/api/v1/asgi/{deployment.name}/latest/api/items/3"
        version_path = f"/api/v1/asgi/{deployment.name}/v{deployment.version}/api/items/4"

        id_response = client.put(
            f"{id_path}/api/items/1",
            headers=headers | {"x-client-header": "asgi-id"},
            content=b"alpha",
            params={"search": "one"},
        )
        deployment_response = client.patch(
            f"{deployment_path}/api/items/2",
            headers=headers,
            content=b"beta",
        )
        latest_response = client.request("TRACE", latest_path, headers=headers, content=b"gamma")
        version_response = client.delete(version_path, headers=headers, params=[("tag", "a")])
        assert client.post(f"{deployment_path}/warmup", headers=headers).status_code == 200
        public_response = client.post(f"{public_path}/api/public", content=b"public")

        assert id_response.status_code == 202
        assert id_response.headers["x-forwarded-path"] == "/api/items/1"
        assert id_response.json()["method"] == "PUT"
        assert id_response.json()["path"] == "/api/items/1"
        assert id_response.json()["query_params"] == {"search": ["one"]}
        assert id_response.json()["headers"]["x-client-header"] == ["asgi-id"]
        assert id_response.json()["body"] == "alpha"
        assert deployment_response.status_code == 202
        assert deployment_response.json()["path"] == "/api/items/2"
        assert deployment_response.json()["body"] == "beta"
        assert latest_response.status_code == 202
        assert latest_response.json()["method"] == "TRACE"
        assert latest_response.json()["path"] == "/api/items/3"
        assert version_response.status_code == 202
        assert version_response.json()["method"] == "DELETE"
        assert version_response.json()["path"] == "/api/items/4"
        assert version_response.json()["query_params"] == {"tag": ["a"]}
        assert public_response.status_code == 202
        assert public_response.json()["stub_id"] == public_stub.id
        assert [request.stub_id for request in service.forward_requests] == [
            stub.id,
            stub.id,
            stub.id,
            stub.id,
            public_stub.id,
        ]
        assert [request.stub_id for request in service.serve_requests] == [stub.id]


def _deploy(
    services: ApiServices,
    name: str,
    kind: DeploymentKind,
    *,
    route: str | None = None,
    workspace: str = "default",
    public: bool = False,
) -> tuple[Deployment, StubRecord]:
    owned_workspace(ControlPlaneService(services.context), workspace)
    deployment = services.deployments.deploy(
        DeploymentSpec(
            name=name,
            kind=kind,
            handler=f"{name}:handler",
            route=route,
            metadata={"authorized": not public},
        ),
        workspace=workspace,
    )
    return deployment, _stub_for_deployment(services, deployment.id)


def _stub_for_deployment(services: ApiServices, deployment_id: str) -> StubRecord:
    matches = [
        stub
        for stub in ControlPlaneService(services.context).list_stubs()
        if stub.deployment_id == deployment_id
    ]
    assert len(matches) == 1
    return matches[0]


def _base_host(url: str) -> str:
    return urlsplit(url).netloc


def _auth_headers(services: ApiServices, *, workspace: str = "default") -> dict[str, str]:
    owned_workspace(ControlPlaneService(services.context), workspace)
    raw_token, _record = AuthService(services.context).create_token(
        f"route-test-{workspace}",
        scopes=["read", "write"],
        workspace_id=workspace,
    )
    return {"Authorization": f"Bearer {raw_token}"}
