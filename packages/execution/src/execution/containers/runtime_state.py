"""The Redis state a container owns, released when it reaches a terminal state.

A container leaves behind two kinds of key. Its keep-warm marker tells the
autoscaler not to reap it, and carries no TTL when the workload asked never to
scale to zero — so a container that died any way other than being stopped left a
marker that outlived it. Its connection counter is worse than inert: a non-zero
count pins that container, and the stub total it contributes to pins the whole
deployment at its maximum, which bills.

Releasing belongs to the transition rather than to the caller that happened to
know both. Every previous release site was a caller that remembered.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from coordination.redis_client import RedisClient
from shared.workload_keys import (
    pod_container_connections_key,
    pod_keep_warm_lock_key,
    pod_total_connections_key,
)

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


class ContainerRuntimeStateRepository(Protocol):
    def release(self, *, workspace_id: str, stub_id: str, container_id: str) -> None: ...


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


__all__ = [
    "ContainerRuntimeStateRepository",
    "RedisContainerRuntimeStateRepository",
]
