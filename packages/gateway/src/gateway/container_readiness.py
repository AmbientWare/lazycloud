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

from dataclasses import dataclass

from coordination.redis_client import AsyncRedisClient, RedisWireScalar
from execution.containers.readiness import (
    DEFAULT_READINESS_CACHE_TTL_MS,
    DEFAULT_READINESS_PROBE_TIMEOUT_SECONDS,
)
from networking.async_http import AsyncBackendHttpClient, AsyncBackendHttpError
from shared.workload_keys import container_readiness_key

_READY = "1"
_NOT_READY = "0"
# The same range the checkpoint readiness probe accepts. A redirect is a serving
# application answering, and a probe that demanded 200 would call a workload
# unready for routing a request it would have handled.
_HTTP_READY_MIN_STATUS = 200
_HTTP_READY_MAX_STATUS = 400


def _decoded(value: RedisWireScalar) -> str:
    return value.decode() if isinstance(value, bytes) else str(value)


@dataclass(slots=True)
class AsyncRedisContainerReadiness:
    redis: AsyncRedisClient
    client: AsyncBackendHttpClient
    timeout_seconds: float = DEFAULT_READINESS_PROBE_TIMEOUT_SECONDS
    cache_ttl_ms: int = DEFAULT_READINESS_CACHE_TTL_MS

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
        if not address:
            return False
        # Keyed by what was probed rather than by the container: a pod serving two
        # proxied ports would otherwise let a verdict from one answer for the other.
        key = self.redis.key(container_readiness_key(container_id, port=port, path=health_path))
        cached = await self.redis.get(key)
        if cached is not None:
            return _decoded(cached) == _READY
        try:
            if health_path:
                response = await self.client.open_stream(
                    address=address,
                    route_id=route_id,
                    method="GET",
                    path=health_path,
                    headers={},
                    body=b"",
                    timeout_seconds=self.timeout_seconds,
                    resource=f"container {container_id} readiness",
                )
                ready = _HTTP_READY_MIN_STATUS <= response.status_code < _HTTP_READY_MAX_STATUS
                await response.close()
            else:
                await self.client.probe_connection(
                    address=address,
                    route_id=route_id,
                    timeout_seconds=self.timeout_seconds,
                    resource=f"container {container_id} readiness",
                )
                ready = True
        except (AsyncBackendHttpError, ValueError):
            # A route the resolver cannot place is the ordinary shape of a container
            # that registered and then died, and an address the parser rejects came
            # from the address map. Neither is this process malfunctioning, so
            # neither may escape a probe and turn a routing decision into a failed
            # request.
            ready = False
        await self.redis.set(key, _READY if ready else _NOT_READY, px=self.cache_ttl_ms)
        return ready


__all__ = ["AsyncRedisContainerReadiness"]
