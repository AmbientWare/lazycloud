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


RENEW_TOKEN_LOCK_SCRIPT = """
local current = redis.call("GET", KEYS[1])
if current ~= ARGV[1] then
    return 0
end
redis.call("EXPIRE", KEYS[1], ARGV[2])
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


def renew_token_lock(
    redis: RedisClient,
    key: str,
    token: str,
    *,
    ttl_seconds: int,
) -> bool:
    """Extend the lock this token still owns, or report that it is gone.

    Acquisition cannot double as renewal: it is ``nx``, so a holder re-acquiring
    its own live lock fails and concludes it lost something it still has. A
    holder that cannot renew keeps its work only until the lease expires, which
    is what lets another take over when it dies rather than when it is merely
    slow.
    """

    if ttl_seconds <= 0:
        raise ValueError("token-lock TTL must be greater than zero")
    if not token:
        raise ValueError("token-lock token is required")
    return redis.eval_int(RENEW_TOKEN_LOCK_SCRIPT, 1, key, token, ttl_seconds) == 1


def release_token_lock(
    redis: RedisClient,
    key: str,
    token: str,
) -> TokenLockReleaseStatus:
    """Release only the lock still owned by ``token`` in one Redis transition."""

    if not token:
        raise ValueError("token-lock token is required")
    return TokenLockReleaseStatus(redis.eval_int(RELEASE_TOKEN_LOCK_SCRIPT, 1, key, token))
