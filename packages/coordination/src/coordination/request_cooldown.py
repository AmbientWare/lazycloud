from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from math import ceil

from coordination.redis_client import RedisClient

_EXTEND_COOLDOWN = """
local current = tonumber(redis.call('GET', KEYS[1]) or '0')
local deadline = tonumber(ARGV[1])
if deadline > current then
    redis.call('SET', KEYS[1], ARGV[1], 'PXAT', ARGV[1])
    return deadline
end
return current
"""


@dataclass(frozen=True, slots=True)
class RedisRequestCooldown:
    redis: RedisClient
    scope: str

    @property
    def key(self) -> str:
        return self.redis.key("request-cooldown", self.scope)

    def blocked_until(self) -> datetime | None:
        value = self.redis.get(self.key)
        if value is None:
            return None
        return datetime.fromtimestamp(int(value) / 1000, tz=UTC)

    def defer_until(self, deadline: datetime) -> datetime:
        milliseconds = ceil(deadline.timestamp() * 1000)
        effective = self.redis.eval_int(_EXTEND_COOLDOWN, 1, self.key, milliseconds)
        return datetime.fromtimestamp(effective / 1000, tz=UTC)
