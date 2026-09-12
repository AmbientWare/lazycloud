from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol
from uuid import uuid4

from shared.timestamps import to_utc, utc_now

from coordination.redis_client import AsyncRedisClient, RedisClient, redis_text
from coordination.redis_serialization import redis_strings

PROCESS_PRESENCE_NAMESPACE = "process-presence"
DEFAULT_PRESENCE_TTL_SECONDS = 45
DEFAULT_PRESENCE_REFRESH_SECONDS = 15.0


class ProcessPresenceReader(Protocol):
    def observing_since(self, *, now: datetime | None = ...) -> datetime | None: ...


@dataclass(frozen=True, slots=True)
class RedisProcessPresence:
    """How long some process of one role has been continuously alive.

    A reconciler that acts on silence needs to know whether anyone was listening
    for it. That is a fact about a different process, often on a different host,
    so it cannot be answered from a local start time: a scheduler up for a week
    knows nothing about the API restart two minutes ago that stopped every
    heartbeat it is now judging.

    Each process publishes its own start time under its own key with a TTL it
    re-arms, so a process that dies stops answering without anything having to
    notice. Readers take the *oldest* live start, because a rolling deploy always
    has a young process and the question is whether some endpoint was reachable,
    not whether every one of them was.
    """

    redis: RedisClient
    role: str

    def observing_since(self, *, now: datetime | None = None) -> datetime | None:
        """The oldest live start time, or None when nothing of this role answers.

        None is not "forever ago". A caller that treats an empty registry as a
        long observation reinstates exactly the failure this exists to prevent,
        so the absence is returned as an absence and left for the caller to
        refuse on.
        """

        current_time = now or utc_now()
        index = _index_key(self.redis, self.role)
        oldest: datetime | None = None
        for member in redis_strings(self.redis.set_members(index)):
            raw = self.redis.get(member)
            if raw is None:
                # Expired under the index. Dropping it here keeps the index from
                # growing by one entry per process restart forever.
                self.redis.set_remove(index, member)
                continue
            started_at = _parse_started_at(redis_text(raw))
            if started_at is None or started_at > current_time:
                continue
            if oldest is None or started_at < oldest:
                oldest = started_at
        return oldest


@dataclass(frozen=True, slots=True)
class AsyncRedisProcessPresence:
    redis: AsyncRedisClient
    role: str
    ttl_seconds: int = DEFAULT_PRESENCE_TTL_SECONDS
    process_id: str = field(default_factory=lambda: str(uuid4()))

    async def publish(self, started_at: datetime) -> None:
        key = _presence_key(self.redis, self.role, self.process_id)
        await self.redis.set(key, to_utc(started_at).isoformat(), ex=self.ttl_seconds)
        await self.redis.set_add(_index_key(self.redis, self.role), key)

    async def withdraw(self) -> None:
        key = _presence_key(self.redis, self.role, self.process_id)
        await self.redis.delete(key)
        await self.redis.set_remove(_index_key(self.redis, self.role), key)


def presence_refresh_interval(ttl_seconds: int) -> float:
    """Refresh well inside the TTL, so one missed pass is not a disappearance."""

    return max(min(DEFAULT_PRESENCE_REFRESH_SECONDS, ttl_seconds / 3), 1.0)


def _index_key(redis: RedisClient | AsyncRedisClient, role: str) -> str:
    return redis.key(PROCESS_PRESENCE_NAMESPACE, _role_name(role), "index")


def _presence_key(redis: RedisClient | AsyncRedisClient, role: str, process_id: str) -> str:
    return redis.key(PROCESS_PRESENCE_NAMESPACE, _role_name(role), process_id)


def _role_name(role: str) -> str:
    name = role.strip()
    if not name:
        raise ValueError("process presence role is required")
    return name


def _parse_started_at(value: str) -> datetime | None:
    try:
        return to_utc(datetime.fromisoformat(value))
    except ValueError:
        return None


__all__ = [
    "DEFAULT_PRESENCE_TTL_SECONDS",
    "PROCESS_PRESENCE_NAMESPACE",
    "AsyncRedisProcessPresence",
    "ProcessPresenceReader",
    "RedisProcessPresence",
    "presence_refresh_interval",
]
