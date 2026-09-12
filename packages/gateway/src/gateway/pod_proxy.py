from __future__ import annotations

import socket
from dataclasses import dataclass

from coordination.redis_client import AsyncRedisClient, RedisWireScalar
from execution.pods.proxy import (
    DEFAULT_POD_PROXY_TIMEOUT_SECONDS,
    PodProxyBackendError,
    PodProxyHttpRequest,
    PodProxyResponseStream,
    PodProxyTarget,
)
from foundation.http import forwarded_request_headers, forwarded_request_path
from networking.async_http import AsyncBackendHttpClient, AsyncBackendHttpError
from networking.dialer import (
    BackendRouteDialer,
)
from shared.routing import parse_backend_route_address
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
class AsyncRedisPodProxyConnectionRepository:
    redis: AsyncRedisClient

    async def container_connections(
        self,
        workspace_id: str,
        stub_id: str,
        container_id: str,
    ) -> int:
        raw = await self.redis.get(self._container_key(workspace_id, stub_id, container_id))
        return _non_negative_int(raw)

    async def increment_container_connections(
        self,
        workspace_id: str,
        stub_id: str,
        container_id: str,
        *,
        keep_warm_seconds: int | None,
    ) -> int:
        return await self.redis.eval_int(
            _INCREMENT_CONTAINER_CONNECTIONS,
            2,
            self._container_key(workspace_id, stub_id, container_id),
            self._keep_warm_key(workspace_id, stub_id, container_id),
            _keep_warm_argument(keep_warm_seconds),
        )

    async def decrement_container_connections(
        self,
        workspace_id: str,
        stub_id: str,
        container_id: str,
        *,
        keep_warm_seconds: int | None,
    ) -> int:
        return await self.redis.eval_int(
            _FINISH_CONTAINER_CONNECTION,
            2,
            self._container_key(workspace_id, stub_id, container_id),
            self._keep_warm_key(workspace_id, stub_id, container_id),
            _keep_warm_argument(keep_warm_seconds),
        )

    async def increment_total_connections(self, workspace_id: str, stub_id: str) -> int:
        return _non_negative_int(await self.redis.increment(self._total_key(workspace_id, stub_id)))

    async def decrement_total_connections(self, workspace_id: str, stub_id: str) -> int:
        return await self._decrement_or_delete(self._total_key(workspace_id, stub_id))

    async def _decrement_or_delete(self, key: str) -> int:
        return await self.redis.eval_int(
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
class AsyncPodProxyHttpClient:
    client: AsyncBackendHttpClient

    async def open_stream(
        self,
        target: PodProxyTarget,
        request: PodProxyHttpRequest,
        *,
        timeout_seconds: float = DEFAULT_POD_PROXY_TIMEOUT_SECONDS,
        connect_timeout_seconds: float | None = None,
    ) -> PodProxyResponseStream:
        route_id = target.route_id or parse_backend_route_address(target.address)[0]
        try:
            return await self.client.open_stream(
                address=target.address,
                route_id=route_id,
                method=request.method,
                path=forwarded_request_path(request.path, request.query_params),
                headers=forwarded_request_headers(request.headers),
                body=request.body,
                timeout_seconds=timeout_seconds,
                connect_timeout_seconds=connect_timeout_seconds,
                resource="pod",
            )
        except (AsyncBackendHttpError, ValueError) as exc:
            raise PodProxyBackendError(f"pod proxy backend request failed: {exc}") from exc


@dataclass(slots=True)
class PodProxySocketClient:
    route_dialer: BackendRouteDialer

    def open_socket(
        self,
        target: PodProxyTarget,
        *,
        timeout_seconds: float = DEFAULT_POD_PROXY_TIMEOUT_SECONDS,
    ) -> socket.socket:
        route_id = target.route_id or parse_backend_route_address(target.address)[0]
        if not route_id:
            raise ConnectionError("Pod request requires an authorized backend route")
        connection = self.route_dialer.dial_backend_route(route_id, timeout_seconds=timeout_seconds)
        connection.settimeout(timeout_seconds)
        return connection


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
