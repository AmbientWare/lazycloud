from __future__ import annotations

import socket
import threading
import time
from collections.abc import AsyncIterator, Iterator, Sequence
from contextlib import ExitStack
from dataclasses import dataclass, field, replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from uuid import NAMESPACE_DNS, uuid5

import pytest
from api.fastapi_app import create_app
from api.server.services import ApiServices
from control.service import ControlPlaneService, StubKind, StubRecord
from database.records.apps import AppRecord
from database.repositories.apps import AppRepository, StubRepository
from database.repositories.execution import PodExecutionRepository
from database.repositories.orchestration import ContainerRepository
from execution.pods.proxy import (
    PINNED_SANDBOX_CONNECT_TIMEOUT_SECONDS,
    PodProxyBackendError,
    PodProxyHttpRequest,
    PodProxyResponseStream,
    PodProxyTarget,
)
from fastapi.testclient import TestClient
from identity.auth import AuthService
from scheduler.fleet import SchedulerContainerStatus
from scheduler.state import SchedulerContainerAddressMap, SchedulerContainerState
from shared.containers import ContainerRecord, ContainerStatus
from shared.deployment_records import Deployment, DeploymentSpec
from shared.deployments import DeploymentKind
from shared.routing import AgentBackendRoute, BackendRouteState
from starlette.websockets import WebSocketDisconnect
from tests.service_fixtures import owned_workspace
from tests.url_constants import TEST_DOMAIN, TEST_URL
from websockets.sync.server import ServerConnection, serve
from websockets.typing import Subprotocol

BASE_URL = TEST_URL


@pytest.fixture
def client_stack() -> Iterator[ExitStack]:
    with ExitStack() as stack:
        yield stack


def test_pod_id_proxy_preserves_request_and_selects_port_ready_container(
    isolated_services: ApiServices,
    client_stack: ExitStack,
) -> None:
    control = ControlPlaneService(isolated_services.context)
    stub = control.create_stub("web", kind=StubKind.Pod)
    missing_port = _create_container(isolated_services, stub, "missing-port")
    busy = _create_container(isolated_services, stub, "busy")
    selected = _create_container(isolated_services, stub, "selected")
    scheduler = _FakeSchedulerContainers.running(
        missing_port,
        busy,
        selected,
        address_maps={
            missing_port.id: {9090: "10.0.0.9:9090"},
            busy.id: {8080: "10.0.0.1:8080"},
            selected.id: {8080: "10.0.0.2:8080"},
        },
    )
    connections = _RecordingConnections(active={missing_port.id: 0, busy.id: 5, selected.id: 1})
    proxy_client = _RecordingProxyClient()
    service = replace(
        isolated_services.pod_service,
        async_scheduler_containers=scheduler,
        async_pod_proxy_http_client=proxy_client,
        pod_proxy_connections=connections,
        container_readiness_probe=_ServingContainers(),
    )
    client = client_stack.enter_context(
        TestClient(create_app(isolated_services, pod_service=service))
    )

    response = client.patch(
        f"/pod/id/{stub.id}/8080/api/users",
        params=[("tag", "a"), ("tag", "b")],
        headers=_auth_headers(isolated_services) | {"x-client-header": "kept"},
        content=b"payload",
    )

    assert response.status_code == 209
    assert response.headers["x-target-container"] == selected.id
    assert len(proxy_client.calls) == 1
    target, request = proxy_client.calls[0]
    assert target == PodProxyTarget(container_id=selected.id, address="10.0.0.2:8080")
    assert request.stub_id == stub.id
    assert request.method == "PATCH"
    assert request.path == "/api/users"
    assert request.query_params == {"tag": ["a", "b"]}
    assert request.headers["x-client-header"] == ["kept"]
    assert request.body == b"payload"
    assert proxy_client.connect_timeouts == [None]
    assert connections.events == [
        f"total+:{stub.id}",
        f"container+:{selected.id}",
        f"container-:{selected.id}",
        f"total-:{stub.id}",
    ]
    assert connections.active[selected.id] == 1


def test_pod_proxy_records_demand_before_waiting_for_scale_from_zero(
    isolated_services: ApiServices,
    client_stack: ExitStack,
) -> None:
    control = ControlPlaneService(isolated_services.context)
    stub = control.create_stub("cold-web", kind=StubKind.Pod)
    container = _create_container(isolated_services, stub, "cold")
    scheduler = _FakeSchedulerContainers()
    connections = _WakeOnDemandConnections(
        scheduler=scheduler,
        container=container,
        port=8080,
    )
    proxy_client = _RecordingProxyClient()
    service = replace(
        isolated_services.pod_service,
        async_scheduler_containers=scheduler,
        async_pod_proxy_http_client=proxy_client,
        pod_proxy_connections=connections,
        container_readiness_probe=_ServingContainers(),
        pod_proxy_start_timeout_seconds=0.1,
        poll_interval_seconds=0.0,
    )
    client = client_stack.enter_context(
        TestClient(create_app(isolated_services, pod_service=service))
    )

    response = client.get(
        f"/pod/id/{stub.id}/8080/health",
        headers=_auth_headers(isolated_services),
    )

    assert response.status_code == 209
    assert connections.events == [
        f"total+:{stub.id}",
        f"container+:{container.id}",
        f"container-:{container.id}",
        f"total-:{stub.id}",
    ]
    assert proxy_client.calls[0][0].container_id == container.id


@pytest.mark.parametrize(
    ("stub_kind", "route_mode"),
    [
        (StubKind.Pod, "private"),
        (StubKind.Sandbox, "private"),
        (StubKind.Sandbox, "public"),
        (StubKind.Sandbox, "host"),
    ],
)
def test_pod_websocket_proxies_subprotocol_text_binary_and_balances_demand(
    isolated_services: ApiServices,
    client_stack: ExitStack,
    monkeypatch: pytest.MonkeyPatch,
    stub_kind: StubKind,
    route_mode: str,
) -> None:
    if route_mode == "host":
        monkeypatch.setattr(isolated_services.gateway_settings, "public_http_url", BASE_URL)
    control = ControlPlaneService(isolated_services.context)
    stub = control.create_stub(
        f"socket-{stub_kind.value}-{route_mode}",
        kind=stub_kind,
        public=route_mode in {"public", "host"},
    )
    container = _create_container(isolated_services, stub, "socket")
    if stub_kind is StubKind.Sandbox:
        _store_sandbox_exposure(
            isolated_services,
            container,
            port=8080,
            public=route_mode in {"public", "host"},
        )
    scheduler = _FakeSchedulerContainers.running(
        container,
        address_maps={container.id: {8080: "route:socket"}},
    )
    connections = _RecordingConnections()
    backend_events: list[tuple[str, str | None, str | bytes]] = []
    backend_errors: list[Exception] = []
    backend_port = _available_loopback_port()
    listener = socket.create_server(("127.0.0.1", backend_port))

    def echo_backend(websocket: ServerConnection) -> None:
        try:
            request = websocket.request
            if request is None:
                raise RuntimeError("websocket backend request is unavailable")
            first = websocket.recv()
            backend_events.append((request.path, websocket.subprotocol, first))
            websocket.send(f"echo:{first}")
            second = websocket.recv()
            backend_events.append((request.path, websocket.subprotocol, second))
            websocket.send(second)
            if websocket.recv() != "close":
                raise AssertionError("expected explicit websocket close request")
            websocket.close(code=1000, reason="complete")
        except Exception as exc:  # pragma: no cover - reported on the test thread
            backend_errors.append(exc)

    backend_server = serve(
        echo_backend,
        sock=listener,
        subprotocols=[Subprotocol("pod.echo.v1")],
    )
    backend_thread = threading.Thread(target=backend_server.serve_forever, daemon=True)
    backend_thread.start()
    socket_client = _LoopbackSocketClient(backend_port)
    service = replace(
        isolated_services.pod_service,
        async_scheduler_containers=scheduler,
        async_pod_proxy_http_client=_RecordingProxyClient(),
        pod_proxy_socket_client=socket_client,
        pod_proxy_connections=connections,
        container_readiness_probe=_ServingContainers(),
    )
    client = client_stack.enter_context(
        TestClient(create_app(isolated_services, pod_service=service))
    )

    try:
        resource = "sandbox" if stub_kind is StubKind.Sandbox else "pod"
        route_id = container.id if stub_kind is StubKind.Sandbox else stub.id
        if route_mode == "public":
            path = f"/{resource}/public/{route_id}/8080/echo?tag=a&tag=b"
            headers = {"x-client-header": "kept"}
        elif route_mode == "host":
            path = "/echo?tag=a&tag=b"
            headers = {
                "host": f"{container.id}-8080.{TEST_DOMAIN}",
                "x-client-header": "kept",
            }
        else:
            path = f"/{resource}/id/{route_id}/8080/echo?tag=a&tag=b"
            headers = _auth_headers(isolated_services) | {"x-client-header": "kept"}
        with client.websocket_connect(
            path,
            headers=headers,
            subprotocols=["pod.echo.v1"],
        ) as websocket:
            assert websocket.accepted_subprotocol == "pod.echo.v1"
            websocket.send_text("hello")
            assert websocket.receive_text() == "echo:hello"
            websocket.send_bytes(b"payload")
            assert websocket.receive_bytes() == b"payload"
            websocket.send_text("close")
            assert websocket.receive() == {
                "type": "websocket.close",
                "code": 1000,
                "reason": "complete",
            }
    finally:
        backend_server.shutdown()
        backend_thread.join(timeout=2)

    deadline = time.monotonic() + 1
    while len(connections.events) < 4 and time.monotonic() < deadline:
        time.sleep(0.01)

    assert backend_errors == []
    assert backend_events == [
        ("/echo?tag=a&tag=b", "pod.echo.v1", "hello"),
        ("/echo?tag=a&tag=b", "pod.echo.v1", b"payload"),
    ]
    assert socket_client.targets[0].container_id == container.id
    assert socket_client.timeouts == [
        (
            PINNED_SANDBOX_CONNECT_TIMEOUT_SECONDS
            if stub_kind is StubKind.Sandbox
            else service.pod_proxy_start_timeout_seconds
        )
    ]
    assert connections.events == [
        f"total+:{stub.id}",
        f"container+:{container.id}",
        f"container-:{container.id}",
        f"total-:{stub.id}",
    ]


def test_pod_websocket_upgrade_withholds_proxy_credentials_from_the_backend(
    isolated_services: ApiServices,
    client_stack: ExitStack,
) -> None:
    control = ControlPlaneService(isolated_services.context)
    stub = control.create_stub("credential-scope", kind=StubKind.Pod)
    container = _create_container(isolated_services, stub, "socket")
    scheduler = _FakeSchedulerContainers.running(
        container,
        address_maps={container.id: {8080: "route:socket"}},
    )
    backend_headers: list[dict[str, str]] = []
    backend_errors: list[Exception] = []
    backend_port = _available_loopback_port()
    listener = socket.create_server(("127.0.0.1", backend_port))

    def echo_backend(websocket: ServerConnection) -> None:
        try:
            request = websocket.request
            if request is None:
                raise RuntimeError("websocket backend request is unavailable")
            backend_headers.append(
                {key.lower(): value for key, value in request.headers.raw_items()}
            )
            websocket.send(f"echo:{websocket.recv()}")
            websocket.close(code=1000, reason="complete")
        except Exception as exc:  # pragma: no cover - reported on the test thread
            backend_errors.append(exc)

    backend_server = serve(echo_backend, sock=listener)
    backend_thread = threading.Thread(target=backend_server.serve_forever, daemon=True)
    backend_thread.start()
    service = replace(
        isolated_services.pod_service,
        async_scheduler_containers=scheduler,
        async_pod_proxy_http_client=_RecordingProxyClient(),
        pod_proxy_socket_client=_LoopbackSocketClient(backend_port),
        pod_proxy_connections=_RecordingConnections(),
        container_readiness_probe=_ServingContainers(),
    )
    client = client_stack.enter_context(
        TestClient(create_app(isolated_services, pod_service=service))
    )

    try:
        with client.websocket_connect(
            f"/pod/id/{stub.id}/8080/echo",
            headers=_auth_headers(isolated_services)
            | {
                "proxy-authorization": "Basic cHJveHktY3JlZGVudGlhbA==",
                "x-client-header": "kept",
            },
        ) as websocket:
            websocket.send_text("hello")
            assert websocket.receive_text() == "echo:hello"
    finally:
        backend_server.shutdown()
        backend_thread.join(timeout=2)

    assert backend_errors == []
    assert backend_headers[0].get("x-client-header") == "kept"
    assert "proxy-authorization" not in backend_headers[0]


def test_pinned_sandbox_routes_never_wait_or_fall_through_to_a_sibling(
    isolated_services: ApiServices,
    client_stack: ExitStack,
) -> None:
    stub = ControlPlaneService(isolated_services.context).create_stub(
        "pinned-siblings",
        kind=StubKind.Sandbox,
    )
    first = _create_container(isolated_services, stub, "first")
    second = _create_container(isolated_services, stub, "second")
    _store_sandbox_exposure(isolated_services, first, port=8080, public=False)
    _store_sandbox_exposure(isolated_services, second, port=8080, public=False)
    scheduler = _FakeSchedulerContainers.running(
        first,
        second,
        address_maps={
            first.id: {8080: "10.0.4.1:8080"},
            second.id: {8080: "10.0.4.2:8080"},
        },
    )
    proxy_client = _RecordingProxyClient()
    connections = _RecordingConnections()
    service = replace(
        isolated_services.pod_service,
        async_scheduler_containers=scheduler,
        async_pod_proxy_http_client=proxy_client,
        pod_proxy_connections=connections,
        container_readiness_probe=_ServingContainers(),
        pod_proxy_start_timeout_seconds=10,
    )
    client = client_stack.enter_context(
        TestClient(create_app(isolated_services, pod_service=service))
    )
    headers = _auth_headers(isolated_services)

    first_response = client.get(f"/sandbox/id/{first.id}/8080", headers=headers)
    second_response = client.get(f"/sandbox/id/{second.id}/8080", headers=headers)

    assert first_response.headers["x-target-container"] == first.id
    assert second_response.headers["x-target-container"] == second.id
    assert proxy_client.connect_timeouts == [
        PINNED_SANDBOX_CONNECT_TIMEOUT_SECONDS,
        PINNED_SANDBOX_CONNECT_TIMEOUT_SECONDS,
    ]
    connection_events = list(connections.events)
    foreign = client.get(
        f"/sandbox/id/{uuid5(NAMESPACE_DNS, 'foreign-sandbox')}/8080",
        headers=headers,
    )
    assert foreign.status_code == 404
    assert connections.events == connection_events

    stopped = isolated_services.containers.get(first.id)
    stopped.status = ContainerStatus.Stopped
    with isolated_services.context.database.session() as session:
        ContainerRepository(session).upsert(stopped)
    stopped_events = list(connections.events)
    started = time.monotonic()
    unavailable = client.get(f"/sandbox/id/{first.id}/8080", headers=headers)
    elapsed = time.monotonic() - started
    assert connections.events == stopped_events
    surviving = client.get(f"/sandbox/id/{second.id}/8080", headers=headers)

    assert unavailable.status_code == 503
    assert elapsed < 0.5
    assert surviving.status_code == 209
    assert surviving.headers["x-target-container"] == second.id


def test_pinned_sandbox_route_metadata_is_ready_exact_and_address_bound(
    isolated_services: ApiServices,
    client_stack: ExitStack,
) -> None:
    stub = ControlPlaneService(isolated_services.context).create_stub(
        "pinned-route-ownership",
        kind=StubKind.Sandbox,
    )
    container = _create_container(isolated_services, stub, "route-owner")
    _store_sandbox_exposure(isolated_services, container, port=8080, public=False)
    scheduler = _FakeSchedulerContainers.running(
        container,
        address_maps={container.id: {8080: "route://owned-route"}},
    )
    route = AgentBackendRoute(
        route_id="owned-route",
        workspace_id=container.workspace_id,
        container_id=container.id,
        port=8080,
        state=BackendRouteState.Ready,
    )
    scheduler.address_maps[container.id] = scheduler.address_maps[container.id].model_copy(
        update={"routes": [route]}
    )
    proxy_client = _RecordingProxyClient()
    service = replace(
        isolated_services.pod_service,
        async_scheduler_containers=scheduler,
        async_pod_proxy_http_client=proxy_client,
        pod_proxy_connections=_RecordingConnections(),
        container_readiness_probe=_ServingContainers(),
    )
    client = client_stack.enter_context(
        TestClient(create_app(isolated_services, pod_service=service))
    )
    headers = _auth_headers(isolated_services)

    ready = client.get(f"/sandbox/id/{container.id}/8080", headers=headers)
    assert ready.status_code == 209
    assert proxy_client.calls[-1][0].route_id == "owned-route"

    invalid_cases = [
        scheduler.address_maps[container.id].model_copy(update={"routes": []}),
        scheduler.address_maps[container.id].model_copy(
            update={"address_map": {8080: "route://sibling-route"}}
        ),
        scheduler.address_maps[container.id].model_copy(
            update={"routes": [route.model_copy(update={"state": BackendRouteState.Opening})]}
        ),
        scheduler.address_maps[container.id].model_copy(
            update={
                "routes": [
                    route.model_copy(
                        update={"container_id": "sibling-container", "route_id": "sibling-route"}
                    )
                ],
                "address_map": {8080: "route://sibling-route"},
            }
        ),
    ]
    successful_calls = len(proxy_client.calls)
    for invalid in invalid_cases:
        scheduler.address_maps[container.id] = invalid
        started = time.monotonic()
        response = client.get(f"/sandbox/id/{container.id}/8080", headers=headers)
        assert response.status_code == 503
        assert time.monotonic() - started < 0.5
    assert len(proxy_client.calls) == successful_calls


def test_pinned_sandbox_backend_failures_are_bounded_and_typed(
    isolated_services: ApiServices,
    client_stack: ExitStack,
) -> None:
    stub = ControlPlaneService(isolated_services.context).create_stub(
        "pinned-backend-failure",
        kind=StubKind.Sandbox,
    )
    container = _create_container(isolated_services, stub, "failed-backend")
    _store_sandbox_exposure(isolated_services, container, port=8080, public=False)
    scheduler = _FakeSchedulerContainers.running(
        container,
        address_maps={container.id: {8080: "127.0.0.1:1"}},
    )
    proxy_client = _RecordingProxyClient(failure=PodProxyBackendError("connect timed out"))
    socket_client = _FailingSocketClient()
    service = replace(
        isolated_services.pod_service,
        async_scheduler_containers=scheduler,
        async_pod_proxy_http_client=proxy_client,
        pod_proxy_socket_client=socket_client,
        pod_proxy_connections=_RecordingConnections(),
        container_readiness_probe=_ServingContainers(),
    )
    client = client_stack.enter_context(
        TestClient(create_app(isolated_services, pod_service=service))
    )
    headers = _auth_headers(isolated_services)

    started = time.monotonic()
    response = client.get(f"/sandbox/id/{container.id}/8080", headers=headers)
    assert response.status_code == 502
    assert time.monotonic() - started < 2.0
    assert proxy_client.connect_timeouts == [PINNED_SANDBOX_CONNECT_TIMEOUT_SECONDS]

    with (
        client.websocket_connect(
            f"/sandbox/id/{container.id}/8080",
            headers=headers,
        ) as websocket,
        pytest.raises(WebSocketDisconnect) as closed,
    ):
        websocket.receive_text()
    assert closed.value.code == 1013
    assert socket_client.timeouts == [PINNED_SANDBOX_CONNECT_TIMEOUT_SECONDS]


def test_sandbox_proxy_supports_id_deployment_and_public_path_forms(
    isolated_services: ApiServices,
    client_stack: ExitStack,
) -> None:
    control = ControlPlaneService(isolated_services.context)
    deployment, stub = _manual_deployment_stub(
        isolated_services,
        name="sandbox-web",
        kind=DeploymentKind.Sandbox,
        stub_kind=StubKind.Sandbox,
    )
    public_stub = control.create_stub("public-sandbox", kind=StubKind.Sandbox, public=True)
    sandbox_container = _create_container(isolated_services, stub, "sandbox")
    public_container = _create_container(isolated_services, public_stub, "public-sandbox")
    _store_sandbox_exposure(isolated_services, sandbox_container, port=7000, public=False)
    _store_sandbox_exposure(isolated_services, public_container, port=7000, public=True)
    scheduler = _FakeSchedulerContainers.running(
        sandbox_container,
        public_container,
        address_maps={
            sandbox_container.id: {7000: "10.0.0.5:7000"},
            public_container.id: {7000: "10.0.0.6:7000"},
        },
    )
    proxy_client = _RecordingProxyClient()
    service = replace(
        isolated_services.pod_service,
        async_scheduler_containers=scheduler,
        async_pod_proxy_http_client=proxy_client,
        pod_proxy_connections=_RecordingConnections(),
        container_readiness_probe=_ServingContainers(),
    )
    client = client_stack.enter_context(
        TestClient(create_app(isolated_services, pod_service=service))
    )
    headers = _auth_headers(isolated_services)

    id_response = client.post(
        f"/sandbox/id/{sandbox_container.id}/7000/run",
        headers=headers,
        content=b"{}",
    )
    latest_response = client.get(f"/sandbox/{deployment.name}/latest/7000/health", headers=headers)
    version_response = client.get(
        f"/sandbox/{deployment.name}/v{deployment.version}/7000/health",
        headers=headers,
    )
    public_response = client.get(f"/sandbox/public/{public_container.id}/7000/status")
    removed_stub_response = client.get(f"/sandbox/id/{stub.id}/7000", headers=headers)

    assert id_response.status_code == 209
    assert latest_response.status_code == 209
    assert version_response.status_code == 209
    assert public_response.status_code == 209
    assert removed_stub_response.status_code == 404
    forwarded = [
        (request.method, request.path, request.stub_id) for _, request in proxy_client.calls
    ]
    assert forwarded == [
        ("POST", "/run", stub.id),
        ("GET", "/health", stub.id),
        ("GET", "/health", stub.id),
        ("GET", "/status", public_stub.id),
    ]


def test_pod_proxy_returns_service_unavailable_when_port_is_missing(
    isolated_services: ApiServices,
    client_stack: ExitStack,
) -> None:
    control = ControlPlaneService(isolated_services.context)
    stub = control.create_stub("missing", kind=StubKind.Pod)
    container = _create_container(isolated_services, stub, "missing")
    scheduler = _FakeSchedulerContainers.running(
        container,
        address_maps={container.id: {8000: "10.0.0.7:8000"}},
    )
    proxy_client = _RecordingProxyClient()
    service = replace(
        isolated_services.pod_service,
        async_scheduler_containers=scheduler,
        async_pod_proxy_http_client=proxy_client,
        pod_proxy_connections=_RecordingConnections(),
        container_readiness_probe=_ServingContainers(),
    )
    client = client_stack.enter_context(
        TestClient(create_app(isolated_services, pod_service=service))
    )

    response = client.get(
        f"/pod/id/{stub.id}/9000",
        headers=_auth_headers(isolated_services),
    )

    assert response.status_code == 503
    assert "port 9000 is not available" in response.json()["detail"]
    assert proxy_client.calls == []


def test_pod_and_sandbox_private_routes_use_token_workspace(
    isolated_services: ApiServices,
    client_stack: ExitStack,
) -> None:
    control = ControlPlaneService(isolated_services.context)
    owned_workspace(control, "pod-owner")
    owned_workspace(control, "pod-other")
    owned_workspace(control, "pod-public")
    pod_stub = control.create_stub("owner-pod", workspace="pod-owner", kind=StubKind.Pod)
    sandbox_stub = control.create_stub(
        "owner-sandbox",
        workspace="pod-owner",
        kind=StubKind.Sandbox,
    )
    public_stub = control.create_stub(
        "public-pod",
        workspace="pod-public",
        kind=StubKind.Pod,
        public=True,
    )
    pod_container = _create_container(isolated_services, pod_stub, "pod")
    sandbox_container = _create_container(isolated_services, sandbox_stub, "sandbox")
    _store_sandbox_exposure(isolated_services, sandbox_container, port=8080, public=False)
    public_container = _create_container(isolated_services, public_stub, "public")
    scheduler = _FakeSchedulerContainers.running(
        pod_container,
        sandbox_container,
        public_container,
        address_maps={
            pod_container.id: {8080: "10.0.1.1:8080"},
            sandbox_container.id: {8080: "10.0.1.2:8080"},
            public_container.id: {8080: "10.0.1.3:8080"},
        },
    )
    proxy_client = _RecordingProxyClient()
    service = replace(
        isolated_services.pod_service,
        async_scheduler_containers=scheduler,
        async_pod_proxy_http_client=proxy_client,
        pod_proxy_connections=_RecordingConnections(),
        container_readiness_probe=_ServingContainers(),
    )
    client = client_stack.enter_context(
        TestClient(create_app(isolated_services, pod_service=service))
    )
    owner_headers = _auth_headers(isolated_services, workspace="pod-owner")
    other_headers = _auth_headers(isolated_services, workspace="pod-other")

    for path in [
        f"/pod/id/{pod_stub.id}/8080",
        f"/sandbox/id/{sandbox_container.id}/8080",
    ]:
        response = client.get(path, headers=other_headers)
        assert response.status_code == 404

    pod_response = client.get(f"/pod/id/{pod_stub.id}/8080", headers=owner_headers)
    sandbox_response = client.get(
        f"/sandbox/id/{sandbox_container.id}/8080",
        headers=owner_headers,
    )
    public_response = client.get(f"/pod/public/{public_stub.id}/8080")

    assert pod_response.status_code == 209
    assert sandbox_response.status_code == 209
    assert public_response.status_code == 209
    assert [request.stub_id for _, request in proxy_client.calls] == [
        pod_stub.id,
        sandbox_stub.id,
        public_stub.id,
    ]


def test_cross_workspace_public_app_does_not_publish_a_private_sandbox(
    isolated_services: ApiServices,
    client_stack: ExitStack,
) -> None:
    control = ControlPlaneService(isolated_services.context)
    owned_workspace(control, "sandbox-owner")
    foreign_workspace = owned_workspace(control, "foreign-app-owner")
    stub = control.create_stub(
        "private-sandbox",
        workspace="sandbox-owner",
        kind=StubKind.Sandbox,
    )
    with isolated_services.context.database.session() as session:
        foreign_app = AppRepository(session).upsert(
            AppRecord(
                id=str(uuid5(NAMESPACE_DNS, "foreign-public-app")),
                workspace_id=foreign_workspace.id,
                name="foreign-public-app",
                public=True,
            )
        )
        StubRepository(session).upsert(stub.model_copy(update={"app_id": foreign_app.id}))
    container = _create_container(isolated_services, stub, "private")
    _store_sandbox_exposure(isolated_services, container, port=8080, public=False)
    scheduler = _FakeSchedulerContainers.running(
        container,
        address_maps={container.id: {8080: "10.0.1.9:8080"}},
    )
    proxy_client = _RecordingProxyClient()
    service = replace(
        isolated_services.pod_service,
        async_scheduler_containers=scheduler,
        async_pod_proxy_http_client=proxy_client,
        pod_proxy_connections=_RecordingConnections(),
        container_readiness_probe=_ServingContainers(),
    )
    client = client_stack.enter_context(
        TestClient(create_app(isolated_services, pod_service=service))
    )

    public_response = client.get(f"/sandbox/public/{container.id}/8080")
    private_response = client.get(
        f"/sandbox/id/{container.id}/8080",
        headers=_auth_headers(isolated_services, workspace="sandbox-owner"),
    )

    assert public_response.status_code == 404
    assert private_response.status_code == 209
    assert len(proxy_client.calls) == 1


@dataclass(frozen=True, slots=True)
class _ServingContainers:
    """Readiness is not what these tests vary.

    They exercise port selection, header and body preservation, and connection
    accounting, so every backend that has an address answers. The cases that turn
    a backend down live in `packages/execution/tests/test_pod_readiness_routing.py`.
    """

    async def is_ready(
        self,
        *,
        container_id: str,
        stub_id: str,
        address: str,
        route_id: str,
        port: int,
        health_path: str = "",
    ) -> bool:
        _ = container_id, stub_id, route_id, port, health_path
        return bool(address)


@dataclass(slots=True)
class _RecordingProxyClient:
    calls: list[tuple[PodProxyTarget, PodProxyHttpRequest]] = field(default_factory=list)
    connect_timeouts: list[float | None] = field(default_factory=list)
    failure: Exception | None = None

    async def open_stream(
        self,
        target: PodProxyTarget,
        request: PodProxyHttpRequest,
        *,
        timeout_seconds: float = 175.0,
        connect_timeout_seconds: float | None = None,
    ) -> PodProxyResponseStream:
        _ = timeout_seconds
        self.calls.append((target, request))
        self.connect_timeouts.append(connect_timeout_seconds)
        if self.failure is not None:
            raise self.failure
        return _RecordedResponseStream(
            status_code=209,
            headers={
                "content-type": ["text/plain"],
                "x-target-container": [target.container_id],
            },
            body=f"{request.method} {request.path}".encode(),
        )


@dataclass(slots=True)
class _RecordedResponseStream:
    status_code: int
    headers: dict[str, list[str]]
    body: bytes

    async def iter_chunks(self) -> AsyncIterator[bytes]:
        yield self.body

    async def close(self) -> None:
        return None


@dataclass(slots=True)
class _LoopbackSocketClient:
    port: int
    targets: list[PodProxyTarget] = field(default_factory=list)
    timeouts: list[float] = field(default_factory=list)

    def open_socket(
        self,
        target: PodProxyTarget,
        *,
        timeout_seconds: float = 175.0,
    ) -> socket.socket:
        self.targets.append(target)
        self.timeouts.append(timeout_seconds)
        return socket.create_connection(
            ("127.0.0.1", self.port),
            timeout=timeout_seconds,
        )


@dataclass(slots=True)
class _FailingSocketClient:
    timeouts: list[float] = field(default_factory=list)

    def open_socket(
        self,
        target: PodProxyTarget,
        *,
        timeout_seconds: float = 175.0,
    ) -> socket.socket:
        _ = target
        self.timeouts.append(timeout_seconds)
        raise TimeoutError("connect timed out")


@dataclass(slots=True)
class _RecordingConnections:
    active: dict[str, int] = field(default_factory=dict)
    events: list[str] = field(default_factory=list)
    total: dict[str, int] = field(default_factory=dict)

    async def container_connections(
        self,
        workspace_id: str,
        stub_id: str,
        container_id: str,
    ) -> int:
        _ = workspace_id, stub_id
        return self.active.get(container_id, 0)

    async def increment_container_connections(
        self,
        workspace_id: str,
        stub_id: str,
        container_id: str,
        *,
        keep_warm_seconds: int | None,
    ) -> int:
        _ = workspace_id, stub_id, keep_warm_seconds
        self.events.append(f"container+:{container_id}")
        self.active[container_id] = self.active.get(container_id, 0) + 1
        return self.active[container_id]

    async def decrement_container_connections(
        self,
        workspace_id: str,
        stub_id: str,
        container_id: str,
        *,
        keep_warm_seconds: int | None,
    ) -> int:
        _ = workspace_id, stub_id, keep_warm_seconds
        self.events.append(f"container-:{container_id}")
        self.active[container_id] = max(self.active.get(container_id, 0) - 1, 0)
        return self.active[container_id]

    async def increment_total_connections(self, workspace_id: str, stub_id: str) -> int:
        _ = workspace_id
        self.events.append(f"total+:{stub_id}")
        self.total[stub_id] = self.total.get(stub_id, 0) + 1
        return self.total[stub_id]

    async def decrement_total_connections(self, workspace_id: str, stub_id: str) -> int:
        _ = workspace_id
        self.events.append(f"total-:{stub_id}")
        self.total[stub_id] = max(self.total.get(stub_id, 0) - 1, 0)
        return self.total[stub_id]


@dataclass(slots=True, kw_only=True)
class _WakeOnDemandConnections(_RecordingConnections):
    scheduler: _FakeSchedulerContainers
    container: ContainerRecord
    port: int

    async def increment_total_connections(self, workspace_id: str, stub_id: str) -> int:
        count = await _RecordingConnections.increment_total_connections(
            self,
            workspace_id,
            stub_id,
        )
        self.scheduler.states[self.container.id] = SchedulerContainerState(
            container_id=self.container.id,
            stub_id=stub_id,
            workspace_id=self.container.workspace_id,
            worker_id="worker-1",
            status=SchedulerContainerStatus.Running,
        )
        self.scheduler.address_maps[self.container.id] = SchedulerContainerAddressMap(
            container_id=self.container.id,
            address_map={self.port: f"10.0.0.10:{self.port}"},
        )
        return count


@dataclass(slots=True)
class _FakeSchedulerContainers:
    states: dict[str, SchedulerContainerState] = field(default_factory=dict)
    address_maps: dict[str, SchedulerContainerAddressMap] = field(default_factory=dict)

    @classmethod
    def running(
        cls,
        *containers: ContainerRecord,
        address_maps: dict[str, dict[int, str]],
    ) -> _FakeSchedulerContainers:
        return cls(
            states={
                container.id: SchedulerContainerState(
                    container_id=container.id,
                    stub_id=container.stub_id or "",
                    workspace_id=container.workspace_id,
                    worker_id="worker-1",
                    status=SchedulerContainerStatus.Running,
                )
                for container in containers
            },
            address_maps={
                container_id: SchedulerContainerAddressMap(
                    container_id=container_id,
                    address_map=address_map,
                )
                for container_id, address_map in address_maps.items()
            },
        )

    async def get_container_state(self, container_id: str) -> SchedulerContainerState | None:
        return self.states.get(container_id)

    async def list_by_stub(self, stub_id: str) -> list[SchedulerContainerState]:
        return [state for state in self.states.values() if state.stub_id == stub_id]

    async def get_container_address_map(self, container_id: str) -> SchedulerContainerAddressMap:
        return self.address_maps.get(
            container_id,
            SchedulerContainerAddressMap(container_id=container_id),
        )

    async def get_container_address_maps(
        self,
        container_ids: Sequence[str],
    ) -> dict[str, SchedulerContainerAddressMap]:
        return {
            container_id: self.address_maps.get(
                container_id,
                SchedulerContainerAddressMap(container_id=container_id),
            )
            for container_id in container_ids
        }


def _create_container(
    services: ApiServices,
    stub: StubRecord,
    suffix: str,
) -> ContainerRecord:
    container = ContainerRecord(
        id=str(uuid5(NAMESPACE_DNS, f"{stub.id}:{suffix}")),
        name=f"{stub.name}-{suffix}",
        image="container-test",
        command=["python", "-m", "http.server"],
        workspace_id=stub.workspace_id,
        stub_id=stub.id,
        status=ContainerStatus.Running,
    )
    with services.context.database.session() as session:
        ContainerRepository(session).records.upsert(
            container,
            key=container.id,
            workspace_id=stub.workspace_id,
            name=container.name,
            status=container.status.value,
        )
    return container


def _store_sandbox_exposure(
    services: ApiServices,
    container: ContainerRecord,
    *,
    port: int,
    public: bool,
) -> None:
    access = "public" if public else "id"
    url = f"{BASE_URL}/sandbox/{access}/{container.id}/{port}"
    with services.context.database.session() as session:
        PodExecutionRepository(session).urls.upsert(
            container_id=container.id,
            port=port,
            url=url,
        )


def _stub_for_deployment(services: ApiServices, deployment_id: str) -> StubRecord:
    matches = [
        stub
        for stub in ControlPlaneService(services.context).list_stubs()
        if stub.deployment_id == deployment_id
    ]
    assert len(matches) == 1
    return matches[0]


def _manual_deployment_stub(
    services: ApiServices,
    *,
    name: str,
    kind: DeploymentKind,
    stub_kind: StubKind,
) -> tuple[Deployment, StubRecord]:
    deployment = services.deployments.deploy(DeploymentSpec(name=name, kind=kind))
    stub = _stub_for_deployment(services, deployment.id)
    assert stub.kind is stub_kind
    return deployment, stub


def _auth_headers(services: ApiServices, *, workspace: str = "default") -> dict[str, str]:
    raw_token, _record = AuthService(services.context).create_token(
        f"pod-proxy-test-{workspace}",
        scopes=["read", "write"],
        workspace_id=workspace,
    )
    return {"Authorization": f"Bearer {raw_token}"}


def _available_loopback_port() -> int:
    server = ThreadingHTTPServer(("127.0.0.1", 0), BaseHTTPRequestHandler)
    try:
        return server.server_port
    finally:
        server.server_close()
