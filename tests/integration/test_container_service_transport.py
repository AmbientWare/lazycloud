from __future__ import annotations

import json
import threading
from collections.abc import Iterable
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from gateway.container_transport import HttpContainerServiceTransportFactory
from lazycloud.json_contracts import JsonValue, parse_json_object
from networking.dialer import SocketBackendConnector
from scheduler.fleet import SchedulerContainerStatus
from scheduler.state import (
    SchedulerContainerAddress,
    SchedulerContainerAddressMap,
    SchedulerContainerState,
)
from shared.bytes_transport import encode_bytes
from shared.contracts import ContractModel
from shared.routing import AgentBackendRoute, BackendRouteState, BackendRouteTransport
from worker.container_client.control import (
    ContainerServiceClient,
    plan_container_client_connection_options,
)
from worker.container_client.models import (
    ContainerClientConnectionOptions,
    ContainerSandboxExecResponse,
    ContainerServiceMethod,
    ContainerServicePayload,
)


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
class _HttpRequest:
    path: str
    headers: dict[str, str]
    body: dict[str, JsonValue]


@dataclass(slots=True)
class _NonSocketBackendConnection:
    closed: bool = False

    def sendall(self, data: bytes) -> None:
        _ = data

    def close(self) -> None:
        self.closed = True


@dataclass(slots=True)
class _ReadyRouteResolver:
    def get_backend_route(self, route_id: str) -> AgentBackendRoute | None:
        if route_id != "route-worker":
            return None
        return AgentBackendRoute(
            route_id=route_id,
            workspace_id="workspace-1",
            pool_name="default",
            machine_id="machine-1",
            worker_id="worker-1",
            proxy_target="worker.internal:8910",
            transport=BackendRouteTransport.Direct,
            state=BackendRouteState.Ready,
        )


class _RecordingHttpServer(ThreadingHTTPServer):
    requests: list[_HttpRequest]


class _ContainerServiceHttpServer:
    def __init__(self) -> None:
        self.server = _RecordingHttpServer(("127.0.0.1", 0), _ContainerServiceHandler)
        self.server.requests = []
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def address(self) -> str:
        address = self.server.server_address
        host = str(address[0])
        port = int(address[1])
        return f"{host}:{port}"

    @property
    def requests(self) -> list[_HttpRequest]:
        return self.server.requests

    def start(self) -> None:
        self.thread.start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)


class _ContainerServiceHandler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:
        length = int(self.headers.get("content-length", "0"))
        body = parse_json_object(self.rfile.read(length))
        server = self.server
        assert isinstance(server, _RecordingHttpServer)
        server.requests.append(
            _HttpRequest(
                path=self.path,
                headers={key.lower(): value for key, value in self.headers.items()},
                body=body,
            )
        )
        if self.path.endswith("/ContainerStreamLogs/stream"):
            self.send_response(200)
            self.send_header("content-type", "application/x-ndjson")
            self.end_headers()
            self.wfile.write(b'{"msg":"first"}\n')
            self.wfile.flush()
            self.wfile.write(b'{"msg":"second"}\n')
            self.wfile.flush()
            return
        if self.path.endswith("/ContainerSandboxDownloadFile"):
            self._write(
                {
                    "ok": True,
                    "data": {"__bytes_base64__": encode_bytes(b"\xff\x00")},
                }
            )
            return
        self._write({"ok": True})

    def log_message(self, format: str, *args: object) -> None:
        _ = format, args

    def _write(self, payload: dict[str, JsonValue]) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@dataclass(slots=True)
class _SchedulerContainersWithRoute:
    def get_container_state(self, container_id: str) -> SchedulerContainerState | None:
        return SchedulerContainerState(
            container_id=container_id,
            stub_id="stub-1",
            workspace_id="workspace-1",
            worker_id="worker-1",
            status=SchedulerContainerStatus.Running,
        )

    def get_worker_address(self, container_id: str) -> SchedulerContainerAddress | None:
        return SchedulerContainerAddress(
            container_id=container_id,
            address="worker.internal:9001",
            route=AgentBackendRoute(route_id="route-worker"),
        )

    def get_container_address_map(self, container_id: str) -> SchedulerContainerAddressMap:
        return SchedulerContainerAddressMap(container_id=container_id)


@dataclass(slots=True)
class _RecordingTransportFactory:
    transport: _RecordingTransport
    options: list[ContainerClientConnectionOptions] = field(default_factory=list)

    def create_transport(
        self,
        options: ContainerClientConnectionOptions,
    ) -> _RecordingTransport:
        self.options.append(options)
        return self.transport


@dataclass(slots=True)
class _RecordingTransport:
    def unary(
        self,
        method: ContainerServiceMethod,
        request: ContractModel,
        *,
        timeout_seconds: float | None = None,
    ) -> ContainerServicePayload:
        _ = request, timeout_seconds
        if method is ContainerServiceMethod.ContainerSandboxExec:
            return ContainerSandboxExecResponse(pid=42)
        return {"ok": True}

    def stream(
        self,
        method: ContainerServiceMethod,
        request: ContractModel,
        *,
        timeout_seconds: float | None = None,
    ) -> Iterable[ContainerServicePayload]:
        _ = method, request, timeout_seconds
        return ()
