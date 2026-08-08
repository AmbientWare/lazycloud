from __future__ import annotations

import json
import socket
from collections.abc import Iterable, Iterator, Sequence
from contextlib import ExitStack
from typing import Protocol, runtime_checkable
from urllib.parse import urlsplit

import cloudpickle
import pytest
from api.fastapi_app import create_app
from api.server.services import ApiServices
from control.service import ControlPlaneService, StubRecord
from execution.endpoints.dispatch import EndpointDispatchTarget, EndpointResponseStream
from execution.endpoints.service import EndpointIngressDispatchSession
from fastapi import FastAPI
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
    FunctionCronRequest,
    FunctionCronResponse,
    FunctionGetArgsRequest,
    FunctionGetArgsResponse,
    FunctionInvokeBody,
    FunctionInvokeResponse,
    FunctionMonitorRequest,
    FunctionMonitorResponse,
    FunctionSetResultBody,
    FunctionSetResultResponse,
)
from shared.http.taskqueues import (
    StartTaskQueueServeRequest,
    StartTaskQueueServeResponse,
    TaskQueueCompleteBody,
    TaskQueueCompleteResponse,
    TaskQueueInvocationEnvelope,
    TaskQueueMonitorRequest,
    TaskQueueMonitorResponse,
    TaskQueuePopRequest,
    TaskQueuePopResponse,
    TaskQueuePutResponse,
    TaskQueueStateResponse,
)
from starlette.routing import BaseRoute, Mount, Route
from tests.url_constants import TEST_URL

BASE_URL = TEST_URL
_JSON_OBJECT_ADAPTER = TypeAdapter(dict[str, JsonValue])


@pytest.fixture
def client_stack() -> Iterator[ExitStack]:
    with ExitStack() as stack:
        yield stack


class _RouteContext(Protocol):
    path: str
    methods: set[str]


@runtime_checkable
class _EffectiveRouteProvider(Protocol):
    def effective_route_contexts(self) -> Iterator[_RouteContext]: ...


def _http_route_paths(app: FastAPI) -> set[str]:
    return set(_http_routes(app.routes))


def _http_routes(routes: Sequence[BaseRoute], *, prefix: str = "") -> Iterator[str]:
    for route in routes:
        if isinstance(route, _EffectiveRouteProvider):
            for context in route.effective_route_contexts():
                if context.methods:
                    yield context.path
        elif isinstance(route, Route) and route.methods:
            yield prefix + route.path
        elif isinstance(route, Mount):
            yield from _http_routes(route.routes, prefix=prefix + route.path)


class RecordingFunctionService:
    def __init__(self) -> None:
        self.requests: list[FunctionInvokeBody] = []

    def function_invoke(self, request: FunctionInvokeBody) -> FunctionInvokeResponse:
        self.requests.append(request)
        return FunctionInvokeResponse.from_result(task_id=f"fn-{len(self.requests)}")

    def function_invoke_stream(
        self,
        request: FunctionInvokeBody,
        *,
        poll_interval_seconds: float = 0.25,
        keepalive_interval_seconds: float = 5.0,
    ) -> Iterable[FunctionInvokeResponse]:
        _ = poll_interval_seconds, keepalive_interval_seconds
        yield self.function_invoke(request)

    def function_get_args(self, request: FunctionGetArgsRequest) -> FunctionGetArgsResponse:
        raise AssertionError(f"unexpected function_get_args call: {request}")

    def function_set_result(
        self,
        request: FunctionSetResultBody,
    ) -> FunctionSetResultResponse:
        raise AssertionError(f"unexpected function_set_result call: {request}")

    def function_monitor(self, request: FunctionMonitorRequest) -> FunctionMonitorResponse:
        raise AssertionError(f"unexpected function_monitor call: {request}")

    def function_cron(self, request: FunctionCronRequest) -> FunctionCronResponse:
        raise AssertionError(f"unexpected function_cron call: {request}")


class RecordingEndpointService:
    def __init__(self) -> None:
        self.forward_requests: list[EndpointForwardRequest] = []
        self.serve_requests: list[StartEndpointServeRequest] = []

    def forward_endpoint_request(
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

    def start_endpoint_serve(
        self,
        request: StartEndpointServeRequest,
    ) -> StartEndpointServeResponse:
        self.serve_requests.append(request)
        return StartEndpointServeResponse(
            container_id=f"endpoint-{len(self.serve_requests)}",
        )

    def prepare_asgi_websocket(
        self,
        request: EndpointForwardRequest,
    ) -> EndpointIngressDispatchSession:
        raise AssertionError(f"unexpected prepare_asgi_websocket call: {request}")

    def prepare_asgi_http(
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

    def open_asgi_http_stream(
        self,
        session: EndpointIngressDispatchSession,
        request: EndpointForwardRequest,
    ) -> EndpointResponseStream:
        _ = session
        return StaticEndpointResponseStream(self.forward_endpoint_request(request))

    def finish_asgi_http(
        self,
        task_id: str,
        *,
        status_code: int | None = None,
        body_size_bytes: int = 0,
        cancelled: bool = False,
        error: str | None = None,
    ) -> None:
        _ = task_id, status_code, body_size_bytes, cancelled, error

    def open_asgi_websocket_socket(
        self,
        session: EndpointIngressDispatchSession,
    ) -> socket.socket | None:
        _ = session
        return None

    def heartbeat_asgi_websocket(self, task_id: str) -> None:
        raise AssertionError(f"unexpected heartbeat_asgi_websocket call: {task_id}")

    def finish_asgi_websocket(
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

    def iter_chunks(self, chunk_size: int = 64 * 1024) -> Iterable[bytes]:
        _ = chunk_size
        yield self._body

    def close(self) -> None:
        return


class RecordingTaskQueueService:
    def __init__(self) -> None:
        self.put_requests: list[tuple[str, bytes]] = []
        self.serve_requests: list[StartTaskQueueServeRequest] = []

    def task_queue_put(self, stub_id: str, payload: bytes) -> TaskQueuePutResponse:
        self.put_requests.append((stub_id, payload))
        return TaskQueuePutResponse(task_id=f"queue-{len(self.put_requests)}")

    def start_task_queue_serve(
        self,
        request: StartTaskQueueServeRequest,
    ) -> StartTaskQueueServeResponse:
        self.serve_requests.append(request)
        return StartTaskQueueServeResponse(
            container_id=f"taskqueue-{len(self.serve_requests)}",
        )

    def task_queue_pop(self, request: TaskQueuePopRequest) -> TaskQueuePopResponse:
        raise AssertionError(f"unexpected task_queue_pop call: {request}")

    def task_queue_monitor(
        self,
        request: TaskQueueMonitorRequest,
    ) -> TaskQueueMonitorResponse:
        raise AssertionError(f"unexpected task_queue_monitor call: {request}")

    def task_queue_complete(
        self,
        request: TaskQueueCompleteBody,
    ) -> TaskQueueCompleteResponse:
        raise AssertionError(f"unexpected task_queue_complete call: {request}")

    def task_queue_state(self, stub_id: str) -> TaskQueueStateResponse:
        raise AssertionError(f"unexpected task_queue_state call: {stub_id}")


def test_unversioned_invoke_rejects_stopped_latest_without_fallback(
    isolated_services: ApiServices,
    client_stack: ExitStack,
) -> None:
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
    client_stack: ExitStack,
) -> None:
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
    client_stack: ExitStack,
) -> None:
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
    client_stack: ExitStack,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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
    client_stack: ExitStack,
) -> None:
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
    client_stack: ExitStack,
) -> None:
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


def test_generated_task_queue_urls_forward_to_put_and_warmup(
    isolated_services: ApiServices,
    client_stack: ExitStack,
) -> None:
    deployment, stub = _deploy(isolated_services, "jobs", DeploymentKind.TaskQueue)
    _public_deployment, public_stub = _deploy(
        isolated_services,
        "public-jobs",
        DeploymentKind.TaskQueue,
        route="/public-jobs",
        public=True,
    )
    service = RecordingTaskQueueService()
    client = client_stack.enter_context(
        TestClient(create_app(isolated_services, taskqueue_service=service))
    )
    headers = _auth_headers(isolated_services)

    id_path = f"/api/v1/taskqueues/id/{stub.id}"
    deployment_path = f"/api/v1/taskqueues/{deployment.name}/v{deployment.version}"
    public_path = f"/api/v1/taskqueues/public/{public_stub.id}"

    id_response = client.post(
        id_path,
        headers=headers,
        json={"args": ["clip.mp4"], "priority": 1},
        params={"count": "2"},
    )
    deployment_response = client.post(
        deployment_path,
        headers=headers,
        json={"kwargs": {"source": "deployment"}},
    )
    warmup_response = client.post(f"{deployment_path}/warmup", headers=headers)
    public_response = client.post(public_path, json={"value": "public"})
    assert id_response.status_code == 200
    assert deployment_response.status_code == 200
    assert warmup_response.status_code == 200
    assert public_response.status_code == 200
    assert [stub_id for stub_id, _payload in service.put_requests] == [
        stub.id,
        stub.id,
        public_stub.id,
    ]
    assert [request.stub_id for request in service.serve_requests] == [stub.id]
    assert cloudpickle.loads(service.put_requests[0][1]) == TaskQueueInvocationEnvelope(
        args=("clip.mp4",),
        kwargs={"priority": 1, "count": 2.0},
    )


def test_private_task_queue_deployed_routes_use_token_workspace(
    isolated_services: ApiServices,
    client_stack: ExitStack,
) -> None:
    deployment, stub = _deploy(
        isolated_services,
        "owner-queue",
        DeploymentKind.TaskQueue,
        workspace="queue-owner",
    )
    service = RecordingTaskQueueService()
    client = client_stack.enter_context(
        TestClient(create_app(isolated_services, taskqueue_service=service))
    )
    other_headers = _auth_headers(isolated_services, workspace="queue-other")

    private_paths = [
        f"/api/v1/taskqueues/id/{stub.id}",
        f"/api/v1/taskqueues/{deployment.name}/latest",
        f"/api/v1/taskqueues/{deployment.name}/v{deployment.version}",
        f"/api/v1/taskqueues/id/{stub.id}/warmup",
        f"/api/v1/taskqueues/{deployment.name}/latest/warmup",
        f"/api/v1/taskqueues/{deployment.name}/v{deployment.version}/warmup",
    ]
    for path in private_paths:
        response = client.post(path, headers=other_headers, json={"value": "blocked"})
        assert response.status_code == 404

    assert service.put_requests == []
    assert service.serve_requests == []


def _deploy(
    services: ApiServices,
    name: str,
    kind: DeploymentKind,
    *,
    route: str | None = None,
    workspace: str = "default",
    public: bool = False,
) -> tuple[Deployment, StubRecord]:
    ControlPlaneService(services.context).upsert_workspace(workspace)
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


def _path(url: str) -> str:
    return urlsplit(url).path


def _host(url: str) -> str:
    return urlsplit(url).netloc


def _base_host(url: str) -> str:
    return urlsplit(url).netloc


def _auth_headers(services: ApiServices, *, workspace: str = "default") -> dict[str, str]:
    ControlPlaneService(services.context).upsert_workspace(workspace)
    raw_token, _record = AuthService(services.context).create_token(
        f"route-test-{workspace}",
        scopes=["read", "write"],
        workspace_id=workspace,
    )
    return {"Authorization": f"Bearer {raw_token}"}
