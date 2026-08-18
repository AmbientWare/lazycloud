"""Dial a container to find out whether its workload is serving.

The verdict is cached for a window rather than recomputed per call. Both routing
paths re-select while they wait — the endpoint dispatcher every 50ms, the pod
proxy every 250ms — so an uncached probe would dial each candidate backend tens
of times a second for every caller queued behind it.

Redis holds the window rather than a process-local dict: several API processes
serve the same stub, and a per-process cache would multiply the probe rate by the
number of them while giving each an independently stale view.

The window is claimed before the dial, not after it. Writing the verdict on the
way out leaves the whole probe duration uncovered, and that duration is longest
for exactly the backend that is not answering — so during a cold start, the case
this exists for, every waiting caller would dial every unready container and hold
a thread for the full timeout doing it.
"""

from __future__ import annotations

from dataclasses import dataclass

from coordination.redis_client import RedisClient
from execution.containers.readiness import (
    DEFAULT_READINESS_CACHE_TTL_MS,
    DEFAULT_READINESS_PROBE_TIMEOUT_SECONDS,
)
from execution.pods.proxy import (
    PodProxyForwardClient,
    PodProxyHttpRequest,
    PodProxySocketClient,
    PodProxyTarget,
)
from shared.workload_keys import container_readiness_key

_READY = "1"
_NOT_READY = "0"
_PROBING = "?"
# The same range the checkpoint readiness probe accepts. A redirect is a serving
# application answering, and a probe that demanded 200 would call a workload
# unready for routing a request it would have handled.
_HTTP_READY_MIN_STATUS = 200
_HTTP_READY_MAX_STATUS = 400


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
            return str(cached) == _READY
        if not self._claim(key):
            # Another caller is dialing this backend right now. Reporting it unready
            # costs one poll interval and keeps the herd off a backend that is by
            # definition slow to answer.
            return False
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

    def _claim(self, key: str) -> bool:
        """Take the right to probe, for no longer than the probe itself can take."""

        return self.redis.set(
            key,
            _PROBING,
            px=max(int(self.timeout_seconds * 1000), 1),
            nx=True,
        )

    def _connect_ready(self, target: PodProxyTarget) -> bool:
        try:
            connection = self.socket_client.open_socket(
                target,
                timeout_seconds=self.timeout_seconds,
            )
        except OSError:
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
        except OSError:
            return False
        return _HTTP_READY_MIN_STATUS <= response.status_code < _HTTP_READY_MAX_STATUS


__all__ = ["RedisContainerReadiness"]
