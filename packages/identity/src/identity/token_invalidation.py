from __future__ import annotations

import logging
from dataclasses import dataclass

from coordination.invalidation import RedisInvalidationGeneration
from coordination.redis_client import REDIS_UNAVAILABLE_ERRORS, RedisClient

logger = logging.getLogger(__name__)

AUTH_TOKEN_INVALIDATION_SCOPE = "auth-tokens"


@dataclass(slots=True)
class AuthTokenInvalidation:
    """Cross-replica invalidation signal for the process-local auth token cache.

    Every token mutation that reduces or changes validity (revoke, disable,
    delete, admin disable, workspace deletion, expiry enforcement) bumps a shared
    Redis generation. Replicas record the generation observed when caching a
    positive lookup and re-read it on every cache hit, so a revoked token stops
    authenticating cluster-wide immediately instead of after the local cache TTL.
    """

    generation: RedisInvalidationGeneration

    @classmethod
    def from_redis(cls, redis: RedisClient) -> AuthTokenInvalidation:
        return cls(RedisInvalidationGeneration(redis=redis, scope=AUTH_TOKEN_INVALIDATION_SCOPE))

    def current_generation(self) -> int | None:
        """Return the current invalidation generation, or None when Redis is unavailable.

        None instructs the verifier to bypass the local cache and authenticate
        against the database, so an unreachable Redis never extends the life of a
        revoked token.
        """
        try:
            return self.generation.current()
        except REDIS_UNAVAILABLE_ERRORS:
            logger.exception("auth token invalidation read failed; bypassing token cache")
            return None

    def emit(self) -> None:
        """Advance the generation so every replica drops its cached token entries.

        A failed emit is logged and swallowed: the validity change is already
        durable in the database, and while Redis is unreachable verifying replicas
        bypass their local caches entirely, so they observe database state directly.
        """
        try:
            self.generation.bump()
        except REDIS_UNAVAILABLE_ERRORS:
            logger.exception(
                "auth token invalidation emit failed; "
                "replicas bypass their token caches while Redis is unavailable"
            )


_PROCESS_TOKEN_INVALIDATION: AuthTokenInvalidation | None = None


def configure_token_invalidation(invalidation: AuthTokenInvalidation | None) -> None:
    """Register this process's cross-replica invalidation signal at composition time.

    Runtime-owned auth services keep their own bounded caches. They share this
    process-level generation coordinator so every replica can reject stale
    positive entries immediately after a credential mutation.
    """
    global _PROCESS_TOKEN_INVALIDATION
    _PROCESS_TOKEN_INVALIDATION = invalidation


def configured_token_invalidation() -> AuthTokenInvalidation | None:
    return _PROCESS_TOKEN_INVALIDATION
