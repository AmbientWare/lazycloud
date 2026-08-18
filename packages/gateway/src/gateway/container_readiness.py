"""Dial a container to find out whether its workload is serving.

The verdict is cached for a window rather than recomputed per call. Both routing
paths re-select while they wait — the endpoint dispatcher every 50ms, the pod
proxy every 250ms — so an uncached probe would dial each candidate backend tens
of times a second for every caller queued behind it.

Redis holds the window rather than a process-local dict: several API processes
serve the same stub, and a per-process cache would multiply the probe rate by the
number of them while giving each an independently stale view.
"""

from __future__ import annotations

from dataclasses import dataclass

from coordination.redis_client import RedisClient
from execution.containers.readiness import (
    DEFAULT_READINESS_CACHE_TTL_MS,
    DEFAULT_READINESS_PROBE_TIMEOUT_SECONDS,
    ContainerHealthCheck,
)
from execution.pods.proxy import PodProxyHttpRequest, PodProxyTarget
from shared.workload_keys import container_readiness_key

from gateway.pod_proxy import PodProxyHttpClient

_READY = "1"
_NOT_READY = "0"
# The same range the checkpoint readiness probe accepts. A redirect is a serving
# application answering, and a probe that demanded 200 would call a workload
# unready for routing a request it would have handled.
_HTTP_READY_MIN_STATUS = 200
_HTTP_READY_MAX_STATUS = 400


@dataclass(slots=True)
class RedisContainerReadiness:
    redis: RedisClient
    client: PodProxyHttpClient
    timeout_seconds: float = DEFAULT_READINESS_PROBE_TIMEOUT_SECONDS
    cache_ttl_ms: int = DEFAULT_READINESS_CACHE_TTL_MS

    def is_ready(
        self,
        *,
        container_id: str,
        stub_id: str,
        address: str,
        route_id: str,
        port: int,
        health_check: ContainerHealthCheck | None = None,
    ) -> bool:
        if not address:
            return False
        key = self.redis.key(container_readiness_key(container_id))
        cached = self.redis.get(key)
        if cached is not None:
            return str(cached) == _READY
        target = PodProxyTarget(
            container_id=container_id,
            address=address,
            route_id=route_id,
        )
        ready = self._probe(target, stub_id=stub_id, port=port, health_check=health_check)
        self.redis.set(key, _READY if ready else _NOT_READY, px=self.cache_ttl_ms)
        return ready

    def _probe(
        self,
        target: PodProxyTarget,
        *,
        stub_id: str,
        port: int,
        health_check: ContainerHealthCheck | None,
    ) -> bool:
        if health_check is not None and health_check.is_http:
            return self._http_ready(target, stub_id=stub_id, port=port, path=health_check.path)
        return self._connect_ready(target)

    def _connect_ready(self, target: PodProxyTarget) -> bool:
        try:
            connection = self.client.open_socket(target, timeout_seconds=self.timeout_seconds)
        except (OSError, RuntimeError, TypeError, ValueError):
            return False
        connection.close()
        return True

    def _http_ready(
        self,
        target: PodProxyTarget,
        *,
        stub_id: str,
        port: int,
        path: str,
    ) -> bool:
        try:
            response = self.client.forward(
                target,
                PodProxyHttpRequest(
                    stub_id=stub_id,
                    container_id=target.container_id,
                    port=port,
                    method="GET",
                    path=path,
                ),
                timeout_seconds=self.timeout_seconds,
                connect_timeout_seconds=self.timeout_seconds,
            )
        except (OSError, RuntimeError, TypeError, ValueError):
            return False
        return _HTTP_READY_MIN_STATUS <= response.status_code < _HTTP_READY_MAX_STATUS


__all__ = ["RedisContainerReadiness"]
