from __future__ import annotations

from coordination.redis_client import AsyncRedisClient, RedisClient

CONSUME_RATE_LIMIT_SCRIPT = """
local count = redis.call("INCR", KEYS[1])
if count == 1 then
    redis.call("EXPIRE", KEYS[1], ARGV[2])
end
if count > tonumber(ARGV[1]) then
    return 0
end
return 1
"""

ACQUIRE_SLOT_SCRIPT = """
redis.call("ZREMRANGEBYSCORE", KEYS[1], "-inf", ARGV[3])
if redis.call("ZCARD", KEYS[1]) >= tonumber(ARGV[1]) then
    return 0
end
redis.call("ZADD", KEYS[1], ARGV[4], ARGV[2])
redis.call("EXPIRE", KEYS[1], ARGV[5])
return 1
"""

RELEASE_SLOT_SCRIPT = """
return redis.call("ZREM", KEYS[1], ARGV[1])
"""


def try_consume(
    redis: RedisClient,
    key: str,
    *,
    limit: int,
    window_seconds: int,
) -> bool:
    """Take one unit from a fixed window, refusing once the window is spent.

    The TTL is set only by the increment that opens the window, so later hits
    never extend it — extending would turn a sustained burst into a permanent
    block, and never expiring would turn the limiter into one.
    """

    _require_window(limit, window_seconds)
    return redis.eval_int(CONSUME_RATE_LIMIT_SCRIPT, 1, key, limit, window_seconds) == 1


async def try_consume_async(
    redis: AsyncRedisClient,
    key: str,
    *,
    limit: int,
    window_seconds: int,
) -> bool:
    _require_window(limit, window_seconds)
    return await redis.eval_int(CONSUME_RATE_LIMIT_SCRIPT, 1, key, limit, window_seconds) == 1


def _require_window(limit: int, window_seconds: int) -> None:
    if limit <= 0:
        raise ValueError("rate-limit limit must be greater than zero")
    if window_seconds <= 0:
        raise ValueError("rate-limit window must be greater than zero")


def try_acquire_slot(
    redis: RedisClient,
    key: str,
    token: str,
    *,
    limit: int,
    ttl_seconds: int,
    now_seconds: float,
) -> bool:
    """Claim one of ``limit`` concurrent slots, fenced by ``token``.

    A holder that never releases stops counting once its deadline passes, so a
    crashed caller cannot hold capacity forever. ``now_seconds`` comes from the
    caller so the deadline arithmetic has one clock.
    """

    if limit <= 0:
        raise ValueError("slot limit must be greater than zero")
    if ttl_seconds <= 0:
        raise ValueError("slot TTL must be greater than zero")
    if not token:
        raise ValueError("slot token is required")
    deadline = now_seconds + ttl_seconds
    return (
        redis.eval_int(
            ACQUIRE_SLOT_SCRIPT,
            1,
            key,
            limit,
            token,
            now_seconds,
            deadline,
            ttl_seconds,
        )
        == 1
    )


def release_slot(redis: RedisClient, key: str, token: str) -> bool:
    """Free the slot held by ``token``; a stranger's token frees nothing."""

    if not token:
        raise ValueError("slot token is required")
    return redis.eval_int(RELEASE_SLOT_SCRIPT, 1, key, token) == 1
