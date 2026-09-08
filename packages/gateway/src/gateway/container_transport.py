from __future__ import annotations

import http.client
import json
import socket
from collections.abc import Iterable
from dataclasses import dataclass, field
from urllib.parse import ParseResult, urlparse

from networking.dialer import (
    BackendRouteDialer,
    BackendRouteDialerConfig,
    BackendRouteResolver,
)
from networking.routing import build_backend_route_dial_plan
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
    route_resolver: BackendRouteResolver | None = None
    route_dialer_config: BackendRouteDialerConfig = field(default_factory=BackendRouteDialerConfig)

    def create_transport(
        self,
        options: ContainerClientConnectionOptions,
    ) -> ContainerServiceTransport:
        return HttpContainerServiceTransport(
            options,
            route_resolver=self.route_resolver,
            route_dialer_config=self.route_dialer_config,
        )


@dataclass(slots=True)
class HttpContainerServiceTransport:
    options: ContainerClientConnectionOptions
    route_resolver: BackendRouteResolver | None = None
    route_dialer_config: BackendRouteDialerConfig = field(default_factory=BackendRouteDialerConfig)

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
    ) -> Iterable[ContainerServiceWireValue]:
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
    ) -> Iterable[ContainerServiceWireValue]:
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
        timeout = timeout_seconds or self.route_dialer_config.timeout_seconds
        if self.options.backend_route_id:
            backend_connection = BackendRouteDialer(
                resolver=self.route_resolver,
                config=self.route_dialer_config,
            ).dial_plan(build_backend_route_dial_plan(self.options.backend_route_id))
            if not isinstance(backend_connection, socket.socket):
                backend_connection.close()
                msg = "container HTTP dialer returned a non-socket connection"
                raise TypeError(msg)
            return _ExistingSocketHttpConnection(backend_connection, timeout=timeout)

        parsed = _parse_service_url(self.options)
        if parsed.scheme == "https":
            return http.client.HTTPSConnection(parsed.hostname or "", parsed.port, timeout=timeout)
        return http.client.HTTPConnection(parsed.hostname or "", parsed.port, timeout=timeout)


class _ExistingSocketHttpConnection(http.client.HTTPConnection):
    def __init__(self, backend_socket: socket.socket, *, timeout: float) -> None:
        super().__init__("backend.route", timeout=timeout)
        self._socket = backend_socket

    def connect(self) -> None:
        self.sock = self._socket
        self.sock.settimeout(self.timeout)


def _parse_service_url(options: ContainerClientConnectionOptions) -> ParseResult:
    raw = options.service_url.strip()
    if not raw:
        msg = "container service URL is required"
        raise ValueError(msg)
    if "://" not in raw:
        scheme = "https" if options.tls else "http"
        raw = f"{scheme}://{raw}"
    parsed = urlparse(raw)
    if parsed.hostname is None or parsed.port is None:
        msg = f"invalid container service URL: {options.service_url}"
        raise ValueError(msg)
    return parsed


__all__ = [
    "HttpContainerServiceTransport",
    "HttpContainerServiceTransportFactory",
]
