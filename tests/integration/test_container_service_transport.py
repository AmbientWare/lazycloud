from __future__ import annotations

from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from time import sleep

import pytest
from gateway.container_transport import HttpContainerServiceTransportFactory
from networking.dialer import BackendRouteDialerConfig, SocketBackendConnector
from shared.compute_policy import MachinePool
from shared.http_transport import HttpChannel
from shared.routing import AgentBackendRoute, BackendRouteState, BackendRouteTransport
from tests.http_server import running_http_server
from worker.container_client.control import (
    ContainerServiceClient,
    plan_container_client_connection_options,
)
from worker.container_client.models import (
    ContainerCheckpointRequest,
    ContainerCheckpointResponse,
    ContainerServiceMethod,
)


def test_checkpoint_requests_can_outlast_connection_timeouts() -> None:
    class CheckpointHandler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            self.rfile.read(int(self.headers["Content-Length"]))
            sleep(0.1)
            body = (
                ContainerCheckpointResponse(ok=True, checkpoint_id="checkpoint-1")
                .model_dump_json()
                .encode()
            )
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), CheckpointHandler)
    address = f"127.0.0.1:{server.server_port}"
    with running_http_server(server):
        transport = HttpContainerServiceTransportFactory(
            route_resolver=_ReadyRouteResolver(address=address),
            route_dialer_config=BackendRouteDialerConfig(timeout_seconds=0.05),
        ).create_transport(
            plan_container_client_connection_options(address, backend_route_id="route-worker")
        )
        request = ContainerCheckpointRequest(container_id="container-1")
        response = transport.unary(
            ContainerServiceMethod.ContainerCheckpoint,
            request,
            timeout_seconds=1.0,
        )
        assert ContainerCheckpointResponse.model_validate(response).checkpoint_id == "checkpoint-1"

        channel = HttpChannel(endpoint=f"http://{address}", timeout_seconds=0.05)
        response = channel.post("/checkpoint", request.model_dump(mode="json"), timeout_seconds=1.0)
        assert ContainerCheckpointResponse.model_validate(response).checkpoint_id == "checkpoint-1"


def test_http_container_service_transport_rejects_non_socket_route_connections(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend_connection = _NonSocketBackendConnection()

    def connect(
        _connector: SocketBackendConnector,
        address: str,
        timeout_seconds: float,
    ) -> _NonSocketBackendConnection:
        assert address == "worker.internal:8910"
        assert timeout_seconds > 0
        return backend_connection

    monkeypatch.setattr(SocketBackendConnector, "connect", connect)
    options = plan_container_client_connection_options(
        "worker.internal:8910",
        backend_route_id="route-worker",
    )
    client = ContainerServiceClient(
        HttpContainerServiceTransportFactory(route_resolver=_ReadyRouteResolver()).create_transport(
            options
        )
    )

    with pytest.raises(TypeError, match="returned a non-socket connection"):
        client.status("ctr-1")

    assert backend_connection.closed


@dataclass(slots=True)
class _NonSocketBackendConnection:
    closed: bool = False

    def sendall(self, data: bytes) -> None:
        _ = data

    def close(self) -> None:
        self.closed = True


@dataclass(slots=True)
class _ReadyRouteResolver:
    address: str = "worker.internal:8910"

    def get_backend_route(self, route_id: str) -> AgentBackendRoute | None:
        if route_id != "route-worker":
            return None
        return AgentBackendRoute(
            route_id=route_id,
            workspace_id="workspace-1",
            pool=MachinePool("default"),
            machine_id="machine-1",
            worker_id="worker-1",
            proxy_target=self.address,
            transport=BackendRouteTransport.Direct,
            state=BackendRouteState.Ready,
        )
