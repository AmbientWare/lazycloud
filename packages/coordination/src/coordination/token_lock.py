from __future__ import annotations

from enum import IntEnum

from coordination.redis_client import RedisClient

RELEASE_TOKEN_LOCK_SCRIPT = """
local current = redis.call("GET", KEYS[1])
if not current then
    return -1
end
if current ~= ARGV[1] then
    return 0
end
redis.call("DEL", KEYS[1])
return 1
"""


class TokenLockReleaseStatus(IntEnum):
    Missing = -1
    TokenMismatch = 0
    Released = 1


def try_acquire_token_lock(
    redis: RedisClient,
    key: str,
    token: str,
    *,
    ttl_seconds: int,
) -> bool:
    """Acquire one token-fenced Redis lock with a bounded lifetime."""

    if ttl_seconds <= 0:
        raise ValueError("token-lock TTL must be greater than zero")
    if not token:
        raise ValueError("token-lock token is required")
    return redis.set(key, token, ex=ttl_seconds, nx=True)


def release_token_lock(
    redis: RedisClient,
    key: str,
    token: str,
) -> TokenLockReleaseStatus:
    """Release only the lock still owned by ``token`` in one Redis transition."""

    if not token:
        raise ValueError("token-lock token is required")
    return TokenLockReleaseStatus(redis.eval_int(RELEASE_TOKEN_LOCK_SCRIPT, 1, key, token))
