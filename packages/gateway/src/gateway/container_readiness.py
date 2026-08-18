"""Dial a container to find out whether its workload is serving.

The verdict is cached for a window rather than recomputed per call. Both routing
paths re-select while they wait — the endpoint dispatcher every 50ms, the pod
proxy every 250ms — so an uncached probe would dial each candidate backend tens
of times a second for every caller queued behind it.

Redis holds the window rather than a process-local dict: several API processes
serve the same stub, and a per-process cache would multiply the probe rate by the
number of them while giving each an independently stale view.

A caller that finds no verdict probes rather than waiting on whichever caller is
already probing. Suppressing the duplicate dial is tempting — it is the cold
start, so every waiting caller misses at once — but the loser of that race has no
verdict to report, and reporting "not ready" is read one layer up as "no capacity
exists", which asks the scheduler for another container. Paying for a duplicate
dial is cheaper than paying for a duplicate container, and the common cold-start
failure is a refused connection, which returns at once rather than at the
timeout.
"""

from __future__ import annotations

import http.client
from dataclasses import dataclass

from coordination.redis_client import RedisClient, RedisWireScalar
from execution.containers.readiness import (
    DEFAULT_READINESS_CACHE_TTL_MS,
    DEFAULT_READINESS_PROBE_TIMEOUT_SECONDS,
)
from execution.pods.proxy import (
    PodProxyForwardClient,
    PodProxyHttpRequest,
    PodProxySocketClient,
    PodProxyTarget,
    PodProxyUnavailable,
)
from networking.dialer import BackendRouteUnavailable
from shared.workload_keys import container_readiness_key

_READY = "1"
_NOT_READY = "0"
# The same range the checkpoint readiness probe accepts. A redirect is a serving
# application answering, and a probe that demanded 200 would call a workload
# unready for routing a request it would have handled.
_HTTP_READY_MIN_STATUS = 200
_HTTP_READY_MAX_STATUS = 400
# What a backend that is not serving looks like from here. A route the resolver
# cannot place is the ordinary shape of a container that registered and then died,
# and a workload that accepts the connection without speaking HTTP answers the
# question just as clearly — neither is this process malfunctioning, so neither
# may escape a probe and turn a routing decision into a failed request.
_UNREACHABLE = (
    OSError,
    BackendRouteUnavailable,
    PodProxyUnavailable,
    http.client.HTTPException,
    # An address the parser rejects came from the address map, not from this code,
    # and names a backend nothing can reach.
    ValueError,
)


def _decoded(value: RedisWireScalar) -> str:
    return value.decode() if isinstance(value, bytes) else str(value)


@dataclass(slots=True)
class RedisContainerReadiness:
    redis: RedisClient
    forward_client: PodProxyForwardClient
    socket_client: PodProxySocketClient
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
        health_path: str = "",
    ) -> bool:
        if not address:
            return False
        # Keyed by what was probed rather than by the container: a pod serving two
        # proxied ports would otherwise let a verdict from one answer for the other.
        key = self.redis.key(container_readiness_key(container_id, port=port, path=health_path))
        cached = self.redis.get(key)
        if cached is not None:
            return _decoded(cached) == _READY
        target = PodProxyTarget(
            container_id=container_id,
            address=address,
            route_id=route_id,
        )
        if health_path:
            ready = self._http_ready(target, stub_id=stub_id, port=port, path=health_path)
        else:
            ready = self._connect_ready(target)
        self.redis.set(key, _READY if ready else _NOT_READY, px=self.cache_ttl_ms)
        return ready

    def _connect_ready(self, target: PodProxyTarget) -> bool:
        try:
            connection = self.socket_client.open_socket(
                target,
                timeout_seconds=self.timeout_seconds,
            )
        except _UNREACHABLE:
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
            response = self.forward_client.forward(
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
        except _UNREACHABLE:
            return False
        return _HTTP_READY_MIN_STATUS <= response.status_code < _HTTP_READY_MAX_STATUS


__all__ = ["RedisContainerReadiness"]
