from __future__ import annotations

import http.client
import json
import socket
from collections.abc import Generator
from dataclasses import dataclass

from networking.dialer import (
    BackendRouteDialer,
)
from pydantic import JsonValue, TypeAdapter
from shared.contracts import ContractModel
from worker.container_client.control import ContainerServiceTransport
from worker.container_client.models import (
    ContainerClientConnectionOptions,
    ContainerServiceMethod,
    ContainerServiceWireValue,
)
from worker.container_client.wire import (
    CONTAINER_SERVICE_HTTP_PREFIX as _CONTAINER_SERVICE_HTTP_PREFIX,
)
from worker.container_client.wire import (
    decode_container_service_wire_value as _decode_container_service_wire_value,
)
from worker.container_client.wire import (
    encode_container_service_wire_value as _encode_container_service_wire_value,
)

_JSON_VALUE: TypeAdapter[JsonValue] = TypeAdapter(JsonValue)


@dataclass(slots=True)
class HttpContainerServiceTransportFactory:
    route_dialer: BackendRouteDialer

    def create_transport(
        self,
        options: ContainerClientConnectionOptions,
    ) -> ContainerServiceTransport:
        return HttpContainerServiceTransport(
            options,
            route_dialer=self.route_dialer,
        )


@dataclass(slots=True)
class HttpContainerServiceTransport:
    options: ContainerClientConnectionOptions
    route_dialer: BackendRouteDialer

    def unary(
        self,
        method: ContainerServiceMethod,
        request: ContractModel,
        *,
        timeout_seconds: float | None = None,
    ) -> ContainerServiceWireValue:
        body = self._post(method, request, timeout_seconds=timeout_seconds, stream=False)
        if not body:
            return {}
        return _decode_container_service_wire_value(_JSON_VALUE.validate_json(body))

    def stream(
        self,
        method: ContainerServiceMethod,
        request: ContractModel,
        *,
        timeout_seconds: float | None = None,
    ) -> Generator[ContainerServiceWireValue, None, None]:
        return self._stream(method, request, timeout_seconds=timeout_seconds)

    def _post(
        self,
        method: ContainerServiceMethod,
        request: ContractModel,
        *,
        timeout_seconds: float | None,
        stream: bool,
    ) -> bytes:
        path = f"{_CONTAINER_SERVICE_HTTP_PREFIX}/{method.value}"
        if stream:
            path = f"{path}/stream"
        payload = json.dumps(
            _encode_container_service_wire_value(request.model_dump(mode="python")),
            separators=(",", ":"),
        ).encode("utf-8")
        connection = self._connection(timeout_seconds)
        headers = {
            "accept": "application/json",
            "content-type": "application/json",
            "content-length": str(len(payload)),
            **self.options.auth_metadata,
        }
        try:
            connection.request("POST", path, body=payload, headers=headers)
            response = connection.getresponse()
            body = response.read()
            if response.status >= 400:
                detail = body.decode("utf-8", errors="replace").strip()
                message = f"container service {method.value} returned HTTP {response.status}"
                if detail:
                    message = f"{message}: {detail}"
                raise RuntimeError(message)
            return body
        finally:
            connection.close()

    def _stream(
        self,
        method: ContainerServiceMethod,
        request: ContractModel,
        *,
        timeout_seconds: float | None,
    ) -> Generator[ContainerServiceWireValue, None, None]:
        path = f"{_CONTAINER_SERVICE_HTTP_PREFIX}/{method.value}/stream"
        payload = json.dumps(
            _encode_container_service_wire_value(request.model_dump(mode="python")),
            separators=(",", ":"),
        ).encode("utf-8")
        connection = self._connection(timeout_seconds)
        headers = {
            "accept": "application/x-ndjson",
            "content-type": "application/json",
            "content-length": str(len(payload)),
            **self.options.auth_metadata,
        }
        try:
            connection.request("POST", path, body=payload, headers=headers)
            response = connection.getresponse()
            if response.status >= 400:
                body = response.read()
                detail = body.decode("utf-8", errors="replace").strip()
                message = f"container service {method.value} returned HTTP {response.status}"
                if detail:
                    message = f"{message}: {detail}"
                raise RuntimeError(message)
            while True:
                line = response.readline()
                if not line:
                    break
                clean = line.strip()
                if clean:
                    yield _decode_container_service_wire_value(_JSON_VALUE.validate_json(clean))
        finally:
            connection.close()

    def _connection(self, timeout_seconds: float | None) -> http.client.HTTPConnection:
        timeout = timeout_seconds or self.route_dialer.config.timeout_seconds
        if not self.options.backend_route_id:
            raise ConnectionError("Container control requires an authorized backend route")
        connection = self.route_dialer.dial_backend_route(
            self.options.backend_route_id,
            timeout_seconds=timeout,
        )
        return _ExistingSocketHttpConnection(connection, timeout=timeout)


class _ExistingSocketHttpConnection(http.client.HTTPConnection):
    def __init__(self, backend_socket: socket.socket, *, timeout: float) -> None:
        super().__init__("backend.route", timeout=timeout)
        self._socket = backend_socket

    def connect(self) -> None:
        self.sock = self._socket
        self.sock.settimeout(self.timeout)


__all__ = [
    "HttpContainerServiceTransport",
    "HttpContainerServiceTransportFactory",
]
