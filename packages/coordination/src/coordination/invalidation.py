from __future__ import annotations

from dataclasses import dataclass

from coordination.redis_client import RedisClient, redis_text

INVALIDATION_NAMESPACE = "invalidation"


@dataclass(slots=True)
class RedisInvalidationGeneration:
    """Monotonic per-scope generation counter for cross-replica cache invalidation.

    Each process records the generation it observed when it filled a local cache
    entry and re-reads the counter on every cache hit. Any difference means another
    replica invalidated the scope, so the entry must be refreshed from the source of
    truth. Correctness does not depend on a live subscriber: the counter lives in
    Redis, and a reset counter reads as a different generation, which safely
    invalidates. Redis errors propagate; callers own outage policy.
    """

    redis: RedisClient
    scope: str

    def key(self) -> str:
        return self.redis.key(INVALIDATION_NAMESPACE, self.scope)

    def current(self) -> int:
        value = self.redis.get(self.key())
        if value is None:
            return 0
        return int(redis_text(value))

    def bump(self) -> int:
        return self.redis.increment(self.key())
