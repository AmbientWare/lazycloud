from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from coordination.redis_client import RedisClient

WAKE_SIGNAL_NAMESPACE = "wake"
WAKE_SIGNAL_PAYLOAD = "1"

COALESCE_WAKE_SIGNAL_SCRIPT = """
if redis.call("LLEN", KEYS[1]) > 0 then
    return 0
end
return redis.call("RPUSH", KEYS[1], ARGV[1])
"""


class WakeSignalPublisher(Protocol):
    def signal(self) -> bool: ...


class WakeSignalWaiter(Protocol):
    def wait(self, *, timeout_seconds: float) -> bool: ...


@dataclass(frozen=True, slots=True)
class RedisWakeSignal:
    """Coalesced Redis semaphore used only to wake a durable-work reconciler."""

    redis: RedisClient
    scope: str
    namespace: str = WAKE_SIGNAL_NAMESPACE

    def key(self) -> str:
        scope = self.scope.strip()
        if not scope:
            raise ValueError("wake signal scope is required")
        return self.redis.key(self.namespace, scope)

    def signal(self) -> bool:
        queued = self.redis.eval_int(
            COALESCE_WAKE_SIGNAL_SCRIPT,
            1,
            self.key(),
            WAKE_SIGNAL_PAYLOAD,
        )
        return queued > 0

    def wait(self, *, timeout_seconds: float) -> bool:
        if timeout_seconds <= 0:
            raise ValueError("wake signal timeout must be greater than zero")
        return (
            self.redis.blocking_list_pop(
                [self.key()],
                timeout=timeout_seconds,
            )
            is not None
        )
