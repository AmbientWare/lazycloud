from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from coordination.redis_client import RedisClient, redis_text
from pydantic import BaseModel, ConfigDict, Field

WIREGUARD_PRESENCE_TTL_SECONDS = 10

_PUBLISH = """
if redis.call("GET", KEYS[1]) ~= ARGV[1] then
    return 0
end
redis.call("SET", KEYS[2], ARGV[2], "EX", ARGV[3])
return 1
"""

_READ_CURRENT = """
local result = {}
for offset = 1, #KEYS, 2 do
    local owner = redis.call("GET", KEYS[offset])
    local payload = redis.call("GET", KEYS[offset + 1])
    if owner and payload and cjson.decode(payload).owner_token == owner then
        table.insert(result, payload)
    end
end
return result
"""

_REMOVE = """
if redis.call("GET", KEYS[1]) ~= ARGV[1] then
    return 0
end
local payload = redis.call("GET", KEYS[2])
if not payload or cjson.decode(payload).owner_token ~= ARGV[1] then
    return 0
end
return redis.call("DEL", KEYS[2])
"""


class WireGuardPeerPresence(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    peer_id: str = Field(min_length=1)
    generation: int = Field(ge=1)


class WireGuardGatewayPresence(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    index: int = Field(ge=0, le=31)
    owner_token: str = Field(min_length=1, repr=False)
    draining: bool = False
    peers: tuple[WireGuardPeerPresence, ...] = ()


@dataclass(frozen=True, slots=True)
class WireGuardGatewayPresenceRepository:
    redis: RedisClient

    def lease_key(self, index: int) -> str:
        return self._key(index, "active")

    def publish(self, presence: WireGuardGatewayPresence) -> bool:
        presence = WireGuardGatewayPresence.model_validate(dict(presence))
        return (
            self.redis.eval_int(
                _PUBLISH,
                2,
                self.lease_key(presence.index),
                self._key(presence.index, "presence"),
                presence.owner_token,
                presence.model_dump_json(),
                WIREGUARD_PRESENCE_TTL_SECONDS,
            )
            == 1
        )

    def list_current(self, indices: Sequence[int]) -> tuple[WireGuardGatewayPresence, ...]:
        keys = [
            key
            for index in sorted(set(indices))
            for key in (self.lease_key(index), self._key(index, "presence"))
        ]
        if not keys:
            return ()
        values = self.redis.eval_scalars(_READ_CURRENT, len(keys), *keys)
        return tuple(
            WireGuardGatewayPresence.model_validate_json(redis_text(value)) for value in values
        )

    def remove(self, index: int, owner_token: str) -> bool:
        if not owner_token:
            raise ValueError("WireGuard gateway owner token is required")
        return (
            self.redis.eval_int(
                _REMOVE,
                2,
                self.lease_key(index),
                self._key(index, "presence"),
                owner_token,
            )
            == 1
        )

    def _key(self, index: int, suffix: str) -> str:
        if not 0 <= index < 32:
            raise ValueError("WireGuard gateway index must be between 0 and 31")
        return self.redis.key("wireguard", "gateway", str(index), suffix)


__all__ = [
    "WIREGUARD_PRESENCE_TTL_SECONDS",
    "WireGuardGatewayPresence",
    "WireGuardGatewayPresenceRepository",
    "WireGuardPeerPresence",
]
