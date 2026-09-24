"""Release keep-warm markers and connection counts at a container's terminal transition.

Markers can have no TTL, and stale connection counts prevent scale-down. Both
must be cleared even when the container exits without an explicit stop request.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Protocol

from coordination.redis_client import RedisClient, RedisWireScalar
from shared.containers import ContainerRecord
from shared.workload_keys import (
    pod_container_connections_key,
    pod_keep_warm_lock_key,
    pod_total_connections_key,
)

LOGGER = logging.getLogger(__name__)

# Clearing the container's own counter has to move the stub total by the same
# amount in the same step. Two round trips would leave the total counting a
# container that no longer exists if the process died between them, and that
# total is what decides whether the deployment may scale down at all.
_RELEASE_CONTAINER_CONNECTIONS = """
local counted = redis.call("GET", KEYS[1])
if not counted then
    return 0
end
redis.call("DEL", KEYS[1])

local held = tonumber(counted)
if not held or held <= 0 then
    return 0
end

local total = redis.call("GET", KEYS[2])
if not total then
    return 0
end

local remaining = tonumber(total) - held
if remaining <= 0 then
    redis.call("DEL", KEYS[2])
    return 0
end

redis.call("SET", KEYS[2], remaining)
return remaining
"""


@dataclass(frozen=True, slots=True)
class PodKeepAlive:
    """What keeps a running pod container from stopping, read from the proxy's counters."""

    connections: int
    """Connections the pod proxy holds open to the container."""

    idle_seconds: int | None
    """Seconds until the keep-warm marker expires; None while a connection holds
    it or when it never expires."""


class ContainerRuntimeStateRepository(Protocol):
    def release(self, *, workspace_id: str, stub_id: str, container_id: str) -> None: ...


class PodKeepAliveReader(Protocol):
    def keep_alive(self, *, workspace_id: str, stub_id: str, container_id: str) -> PodKeepAlive: ...


def release_container_runtime_state(
    repository: ContainerRuntimeStateRepository | None,
    container: ContainerRecord,
) -> None:
    """A Redis failure must not prevent a durable terminal transition."""
    if repository is None or not container.stub_id:
        return
    try:
        repository.release(
            workspace_id=container.workspace_id,
            stub_id=container.stub_id,
            container_id=container.id,
        )
    except Exception:
        LOGGER.warning(
            "releasing container runtime state failed",
            exc_info=True,
            extra={"container_id": container.id},
        )


@dataclass(frozen=True, slots=True)
class RedisContainerRuntimeStateRepository:
    redis: RedisClient

    def release(self, *, workspace_id: str, stub_id: str, container_id: str) -> None:
        """Drop everything this container held, whatever kind of workload it was.

        The keep-warm key is deleted rather than looked up: deleting one that
        does not exist costs nothing, and asking which kind of workload this was
        would mean a stub read on every terminal transition to answer a question
        the delete does not need.
        """
        self.redis.delete(
            self.redis.key(pod_keep_warm_lock_key(workspace_id, stub_id, container_id)),
        )
        self.redis.eval_int(
            _RELEASE_CONTAINER_CONNECTIONS,
            2,
            self.redis.key(pod_container_connections_key(workspace_id, stub_id, container_id)),
            self.redis.key(pod_total_connections_key(workspace_id, stub_id)),
        )

    def keep_alive(self, *, workspace_id: str, stub_id: str, container_id: str) -> PodKeepAlive:
        """The proxy's connection count and the keep-warm marker's remaining life.

        The proxy persists the marker while a connection is open and sets its
        expiry when the last one closes, so a positive TTL is the idle deadline.
        """
        raw = self.redis.get(
            self.redis.key(pod_container_connections_key(workspace_id, stub_id, container_id))
        )
        ttl = self.redis.ttl(
            self.redis.key(pod_keep_warm_lock_key(workspace_id, stub_id, container_id))
        )
        return PodKeepAlive(
            connections=_count(raw),
            idle_seconds=ttl if ttl > 0 else None,
        )


def _count(raw: RedisWireScalar | None) -> int:
    if raw is None:
        return 0
    try:
        return max(int(raw.decode() if isinstance(raw, bytes) else raw), 0)
    except (TypeError, ValueError):
        return 0


__all__ = [
    "ContainerRuntimeStateRepository",
    "PodKeepAlive",
    "PodKeepAliveReader",
    "RedisContainerRuntimeStateRepository",
    "release_container_runtime_state",
]
