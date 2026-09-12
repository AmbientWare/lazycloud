from __future__ import annotations

from dataclasses import dataclass
from math import floor
from uuid import UUID

from shared.agent_connections import AgentConnectionRecord

from coordination.redis_client import RedisClient
from coordination.redis_serialization import dump_model_json, load_model_json

AGENT_CONNECTION_LEASE_TTL_SECONDS = 6

_CLAIM = """
local current = redis.call('HGET', KEYS[1], 'connection_id')
if current then
    if current ~= ARGV[4] then return 0 end
    local generation = redis.call('HGET', KEYS[1], 'credential_generation')
    if #generation > #ARGV[3] or (#generation == #ARGV[3] and generation > ARGV[3]) then
        return 0
    end
elseif ARGV[4] ~= '' then
    return 0
end
local clock = redis.call('TIME')
local now = tonumber(clock[1]) * 1000 + math.floor(tonumber(clock[2]) / 1000)
local ttl = math.min(tonumber(ARGV[5]) - now, tonumber(ARGV[6]))
if ttl <= 0 then return 0 end
redis.call('HSET', KEYS[1], 'record', ARGV[1], 'connection_id', ARGV[2],
    'credential_generation', ARGV[3])
redis.call('PEXPIRE', KEYS[1], ttl)
return 1
"""

_RENEW = """
if redis.call('HGET', KEYS[1], 'record') ~= ARGV[1] then return 0 end
local clock = redis.call('TIME')
local now = tonumber(clock[1]) * 1000 + math.floor(tonumber(clock[2]) / 1000)
local ttl = math.min(tonumber(ARGV[2]) - now, tonumber(ARGV[3]))
if ttl <= 0 then return 0 end
return redis.call('PEXPIRE', KEYS[1], ttl)
"""

_RELEASE = """
if redis.call('HGET', KEYS[1], 'record') ~= ARGV[1] then return 0 end
return redis.call('DEL', KEYS[1])
"""


@dataclass(frozen=True, slots=True)
class RedisAgentConnectionDirectory:
    redis: RedisClient

    def get(self, workspace_id: str, enrollment_id: str) -> AgentConnectionRecord | None:
        value = self.redis.hash_get(self._key(workspace_id, enrollment_id), "record")
        return load_model_json(AgentConnectionRecord, value) if value is not None else None

    def claim(
        self,
        record: AgentConnectionRecord,
        previous_connection_id: str | None = None,
    ) -> bool:
        previous = str(UUID(previous_connection_id)) if previous_connection_id is not None else ""
        return bool(
            self.redis.eval_int(
                _CLAIM,
                1,
                self._record_key(record),
                dump_model_json(record),
                record.connection_id,
                str(record.identity.credential_generation),
                previous,
                floor(record.expires_at.timestamp() * 1000),
                AGENT_CONNECTION_LEASE_TTL_SECONDS * 1000,
            )
        )

    def renew(self, record: AgentConnectionRecord) -> bool:
        return bool(
            self.redis.eval_int(
                _RENEW,
                1,
                self._record_key(record),
                dump_model_json(record),
                floor(record.expires_at.timestamp() * 1000),
                AGENT_CONNECTION_LEASE_TTL_SECONDS * 1000,
            )
        )

    def release(self, record: AgentConnectionRecord) -> bool:
        return bool(
            self.redis.eval_int(_RELEASE, 1, self._record_key(record), dump_model_json(record))
        )

    def _record_key(self, record: AgentConnectionRecord) -> str:
        return self._key(record.identity.workspace_id, record.identity.enrollment_id)

    def _key(self, workspace_id: str, enrollment_id: str) -> str:
        return self.redis.key(
            "agent-connections", str(UUID(workspace_id)), str(UUID(enrollment_id))
        )


__all__ = ["AGENT_CONNECTION_LEASE_TTL_SECONDS", "RedisAgentConnectionDirectory"]
