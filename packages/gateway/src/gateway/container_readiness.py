"""Share in-flight container probes and cache their verdicts across API processes."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from functools import partial

from coordination.redis_client import AsyncRedisClient, RedisWireScalar
from execution.containers.readiness import (
    DEFAULT_READINESS_CACHE_TTL_MS,
    DEFAULT_READINESS_PROBE_TIMEOUT_SECONDS,
)
from networking.async_http import AsyncBackendHttpClient, AsyncBackendHttpError
from shared.workload_keys import container_readiness_key, workload_readiness_stream_name

_READY = "1"
_NOT_READY = "0"
LOGGER = logging.getLogger(__name__)
# The same range the checkpoint readiness probe accepts. A redirect is a serving
# application answering, and a probe that demanded 200 would call a workload
# unready for routing a request it would have handled.
_HTTP_READY_MIN_STATUS = 200
_HTTP_READY_MAX_STATUS = 400

_PUBLISH_VERDICT = """
local previous = redis.call('GET', KEYS[2])
redis.call('SET', KEYS[1], ARGV[1], 'PX', ARGV[2])
redis.call('SET', KEYS[2], ARGV[1], 'EX', 3600)
if previous ~= ARGV[1] then
    redis.call('XADD', KEYS[3], 'MAXLEN', '~', 128, '*', 'container_id', ARGV[3])
    redis.call('EXPIRE', KEYS[3], 3600)
end
return 1
"""


def _decoded(value: RedisWireScalar) -> str:
    return value.decode() if isinstance(value, bytes) else str(value)


@dataclass(slots=True)
class AsyncRedisContainerReadiness:
    redis: AsyncRedisClient
    client: AsyncBackendHttpClient
    timeout_seconds: float = DEFAULT_READINESS_PROBE_TIMEOUT_SECONDS
    cache_ttl_ms: int = DEFAULT_READINESS_CACHE_TTL_MS
    _probes: dict[str, asyncio.Task[bool]] = field(default_factory=dict, init=False)
    _closed: bool = field(default=False, init=False)

    async def close(self) -> None:
        self._closed = True
        probes = list(self._probes.values())
        for probe in probes:
            probe.cancel()
        await asyncio.gather(*probes, return_exceptions=True)

    def _probe_finished(self, key: str, probe: asyncio.Task[bool]) -> None:
        self._probes.pop(key, None)
        if not probe.cancelled() and (error := probe.exception()) is not None:
            LOGGER.error("Container readiness probe failed", exc_info=error)

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
        revision = await self.client.readiness_revision(route_id, address)
        if revision is None:
            await self._publish(key, stub_id, container_id, "unavailable", False)
            return False
        probe_key = f"{key}:{revision}"
        cached = await self.redis.get(key)
        if self._closed:
            raise RuntimeError("Container readiness is closed")
        if cached is not None and _decoded(cached).startswith(f"{revision}:"):
            return _decoded(cached) == f"{revision}:{_READY}"
        probe = self._probes.get(probe_key)
        if probe is None:
            probe = asyncio.create_task(
                self._probe(key, stub_id, revision, container_id, address, route_id, health_path)
            )
            self._probes[probe_key] = probe
            probe.add_done_callback(partial(self._probe_finished, probe_key))
        # A disconnected caller must not cancel readiness for the other callers.
        return await asyncio.shield(probe)

    async def _probe(
        self,
        key: str,
        stub_id: str,
        revision: str,
        container_id: str,
        address: str,
        route_id: str,
        health_path: str,
    ) -> bool:
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
        # An old session's successful probe cannot establish readiness for its replacement.
        if await self.client.readiness_revision(route_id, address) != revision:
            return False
        await self._publish(key, stub_id, container_id, revision, ready)
        return ready

    async def _publish(
        self, key: str, stub_id: str, container_id: str, revision: str, ready: bool
    ) -> None:
        await self.redis.eval_int(
            _PUBLISH_VERDICT,
            3,
            key,
            f"{key}:verdict",
            self.redis.key(workload_readiness_stream_name(stub_id)),
            f"{revision}:{_READY if ready else _NOT_READY}",
            self.cache_ttl_ms,
            container_id,
        )


__all__ = ["AsyncRedisContainerReadiness"]
