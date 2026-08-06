from __future__ import annotations

import http.client
import socket
from dataclasses import dataclass, field

from coordination.redis_client import RedisClient, RedisWireScalar
from execution.pods.proxy import (
    DEFAULT_POD_PROXY_TIMEOUT_SECONDS,
    PodProxyHttpRequest,
    PodProxyHttpResponse,
    PodProxyTarget,
)
from foundation.http import (
    forwarded_request_headers,
    forwarded_request_path,
    grouped_response_headers,
)
from networking.dialer import (
    BackendRouteDialer,
    BackendRouteDialerConfig,
    BackendRouteResolver,
    TailnetPeerResolver,
    TailnetPeerWaiter,
)
from networking.routing import build_backend_route_dial_plan
from shared.routing import parse_backend_route_address
from shared.urls import parse_container_address
from shared.workload_keys import (
    pod_container_connections_key,
    pod_keep_warm_lock_key,
    pod_total_connections_key,
)

_INCREMENT_CONTAINER_CONNECTIONS = """
local count = redis.call("INCR", KEYS[1])
local keep_warm_seconds = tonumber(ARGV[1])
if keep_warm_seconds and keep_warm_seconds > 0 then
    if redis.call("EXISTS", KEYS[2]) == 1 then
        redis.call("PERSIST", KEYS[2])
    end
elseif keep_warm_seconds == 0 then
    redis.call("DEL", KEYS[2])
end
return count
"""

_FINISH_CONTAINER_CONNECTION = """
local value = redis.call("GET", KEYS[1])
if not value then
    return 0
end

local count = tonumber(value)
if count <= 1 then
    redis.call("DEL", KEYS[1])
    local keep_warm_seconds = tonumber(ARGV[1])
    if keep_warm_seconds and keep_warm_seconds > 0 then
        redis.call("EXPIRE", KEYS[2], keep_warm_seconds)
    end
    return 0
end

return redis.call("DECR", KEYS[1])
"""

_DECREMENT_OR_DELETE_CONNECTION_COUNTER = """
local value = redis.call("GET", KEYS[1])
if not value then
    return 0
end

local count = tonumber(value)
if count <= 1 then
    redis.call("DEL", KEYS[1])
    return 0
end

return redis.call("DECR", KEYS[1])
"""


@dataclass(slots=True)
class RedisPodProxyConnectionRepository:
    redis: RedisClient

    def container_connections(self, workspace_id: str, stub_id: str, container_id: str) -> int:
        raw = self.redis.get(self._container_key(workspace_id, stub_id, container_id))
        return _non_negative_int(raw)

    def increment_container_connections(
        self,
        workspace_id: str,
        stub_id: str,
        container_id: str,
        *,
        keep_warm_seconds: int | None,
    ) -> int:
        return self.redis.eval_int(
            _INCREMENT_CONTAINER_CONNECTIONS,
            2,
            self._container_key(workspace_id, stub_id, container_id),
            self._keep_warm_key(workspace_id, stub_id, container_id),
            _keep_warm_argument(keep_warm_seconds),
        )

    def decrement_container_connections(
        self,
        workspace_id: str,
        stub_id: str,
        container_id: str,
        *,
        keep_warm_seconds: int | None,
    ) -> int:
        return self.redis.eval_int(
            _FINISH_CONTAINER_CONNECTION,
            2,
            self._container_key(workspace_id, stub_id, container_id),
            self._keep_warm_key(workspace_id, stub_id, container_id),
            _keep_warm_argument(keep_warm_seconds),
        )

    def increment_total_connections(self, workspace_id: str, stub_id: str) -> int:
        return _non_negative_int(self.redis.increment(self._total_key(workspace_id, stub_id)))

    def decrement_total_connections(self, workspace_id: str, stub_id: str) -> int:
        return self._decrement_or_delete(self._total_key(workspace_id, stub_id))

    def _decrement_or_delete(self, key: str) -> int:
        return self.redis.eval_int(
            _DECREMENT_OR_DELETE_CONNECTION_COUNTER,
            1,
            key,
        )

    def _key(self, value: str) -> str:
        return self.redis.key(value)

    def _container_key(self, workspace_id: str, stub_id: str, container_id: str) -> str:
        return self._key(pod_container_connections_key(workspace_id, stub_id, container_id))

    def _keep_warm_key(self, workspace_id: str, stub_id: str, container_id: str) -> str:
        return self._key(pod_keep_warm_lock_key(workspace_id, stub_id, container_id))

    def _total_key(self, workspace_id: str, stub_id: str) -> str:
        return self._key(pod_total_connections_key(workspace_id, stub_id))


@dataclass(slots=True)
class PodProxyHttpClient:
    route_resolver: BackendRouteResolver | None = None
    route_dialer_config: BackendRouteDialerConfig = field(default_factory=BackendRouteDialerConfig)
    tailnet_peer_waiter: TailnetPeerWaiter | None = None
    tailnet_peer_resolver: TailnetPeerResolver | None = None

    def forward(
        self,
        target: PodProxyTarget,
        request: PodProxyHttpRequest,
        *,
        timeout_seconds: float = DEFAULT_POD_PROXY_TIMEOUT_SECONDS,
        connect_timeout_seconds: float | None = None,
    ) -> PodProxyHttpResponse:
        connection = self._connection(
            target,
            timeout_seconds=timeout_seconds,
            connect_timeout_seconds=connect_timeout_seconds,
        )
        try:
            connection.request(
                request.method,
                forwarded_request_path(request.path, request.query_params),
                body=request.body,
                headers=forwarded_request_headers(request.headers),
            )
            response = connection.getresponse()
            return PodProxyHttpResponse(
                status_code=response.status,
                headers=grouped_response_headers(response.getheaders()),
                body=response.read(),
            )
        finally:
            connection.close()

    def open_socket(
        self,
        target: PodProxyTarget,
        *,
        timeout_seconds: float = DEFAULT_POD_PROXY_TIMEOUT_SECONDS,
    ) -> socket.socket:
        timeout = timeout_seconds or self.route_dialer_config.timeout_seconds
        route_id = target.route_id or parse_backend_route_address(target.address)[0]
        if route_id:
            dialer_config = self.route_dialer_config.model_copy(
                update={
                    "timeout_seconds": min(
                        timeout,
                        self.route_dialer_config.timeout_seconds,
                    )
                }
            )
            connection = BackendRouteDialer(
                resolver=self.route_resolver,
                config=dialer_config,
                tailnet_peer_waiter=self.tailnet_peer_waiter,
                tailnet_peer_resolver=self.tailnet_peer_resolver,
            ).dial_plan(build_backend_route_dial_plan(route_id))
            if not isinstance(connection, socket.socket):
                msg = "pod proxy requires a socket backend connection"
                raise TypeError(msg)
            connection.settimeout(timeout)
            return connection

        parsed = parse_container_address(target.address, resource="pod")
        return socket.create_connection(
            (parsed.hostname or "", parsed.port or 80),
            timeout=timeout,
        )

    def _connection(
        self,
        target: PodProxyTarget,
        *,
        timeout_seconds: float,
        connect_timeout_seconds: float | None,
    ) -> http.client.HTTPConnection:
        timeout = timeout_seconds or self.route_dialer_config.timeout_seconds
        connect_timeout = connect_timeout_seconds or timeout
        route_id = target.route_id or parse_backend_route_address(target.address)[0]
        if route_id:
            backend_connection = self.open_socket(
                target,
                timeout_seconds=connect_timeout,
            )
            backend_connection.settimeout(timeout)
            return _RouteHttpConnection(backend_connection, timeout=timeout)

        parsed = parse_container_address(target.address, resource="pod")
        if parsed.scheme == "https":
            connection: http.client.HTTPConnection = http.client.HTTPSConnection(
                parsed.hostname or "",
                parsed.port,
                timeout=connect_timeout,
            )
        else:
            connection = http.client.HTTPConnection(
                parsed.hostname or "",
                parsed.port,
                timeout=connect_timeout,
            )
        connection.connect()
        if connection.sock is not None:
            connection.sock.settimeout(timeout)
        return connection


class _RouteHttpConnection(http.client.HTTPConnection):
    def __init__(self, backend_socket: socket.socket, *, timeout: float) -> None:
        super().__init__("backend.route", timeout=timeout)
        self._socket = backend_socket

    def connect(self) -> None:
        self.sock = self._socket


def _non_negative_int(value: RedisWireScalar | None) -> int:
    if value is None:
        return 0
    if isinstance(value, bytes):
        value = value.decode()
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return 0
    return max(parsed, 0)


def _keep_warm_argument(keep_warm_seconds: int | None) -> int:
    return keep_warm_seconds if keep_warm_seconds is not None else -2
