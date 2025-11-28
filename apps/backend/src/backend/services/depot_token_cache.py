import json
from datetime import datetime, timedelta, timezone

import redis.asyncio as redis
from loguru import logger
from models.depot import DepotProjectToken
from redis.asyncio.lock import Lock

from backend.config import app_config


class DepotTokenCache:
    """Redis-based cache for Depot tokens with automatic TTL expiration."""

    def __init__(self, redis_url: str | None = None):
        """Initialize Redis connection."""
        self.redis_url = redis_url or app_config.REDIS_URL
        self._redis_client: redis.Redis | None = None
        self._key_prefix = "depot:token:"

    async def _get_client(self) -> redis.Redis:
        """Get or create Redis client."""
        if self._redis_client is None:
            self._redis_client = redis.from_url(
                self.redis_url, decode_responses=True, encoding="utf-8"
            )
        return self._redis_client

    def _make_key(self, deployment_id: str) -> str:
        """Create Redis key for a token."""
        return f"{self._key_prefix}{deployment_id}"

    def _make_lock_key(self, deployment_id: str) -> str:
        """Create Redis key for a lock."""
        return f"depot:lock:{deployment_id}"

    async def get_lock(self, deployment_id: str, timeout: float = 10.0) -> Lock:
        """Get a Redis lock for token creation"""
        client = await self._get_client()
        lock_key = self._make_lock_key(deployment_id)
        return Lock(client, lock_key, timeout=timeout, sleep=0.1)

    async def token_exists(self, deployment_id: str) -> bool:
        """Check if a token exists in cache"""
        try:
            client = await self._get_client()
            key = self._make_key(deployment_id)
            exists = await client.exists(key)
            return exists > 0

        except redis.RedisError as e:
            logger.warning(f"Redis error checking token existence: {e}")
            return False

        except Exception as e:
            logger.warning(f"Unexpected error checking token existence: {e}")
            return False

    async def get_token(self, deployment_id: str) -> DepotProjectToken | None:
        """Get a cached token if it exists and hasn't expired"""
        try:
            client = await self._get_client()
            key = self._make_key(deployment_id)

            # Use pipeline to get token and TTL in a single round-trip
            pipe = client.pipeline()
            pipe.get(key)
            pipe.ttl(key)
            results = await pipe.execute()

            token_data = results[0]
            ttl = results[1]

            if not token_data or ttl <= 0:
                # Token doesn't exist or expired
                return None

            # Parse JSON data
            data = json.loads(token_data)

            # Use stored expires_at if available, otherwise calculate from TTL
            if "expires_at" in data:
                expires_at = datetime.fromisoformat(data["expires_at"])
            else:
                # Fallback for old cached tokens without expires_at
                expires_at = datetime.now(timezone.utc) + timedelta(seconds=ttl)

            return DepotProjectToken(
                token=data["token"],
                expires_at=expires_at,
            )

        except redis.RedisError as e:
            logger.warning(f"Redis error getting token: {e}")
            return None

        except (json.JSONDecodeError, KeyError, ValueError) as e:
            logger.warning(f"Error parsing token from cache: {e}")
            return None

        except Exception as e:
            logger.warning(f"Unexpected error getting token from cache: {e}")
            return None

    async def set_token(
        self,
        deployment_id: str,
        token: str,
        expires_at: datetime,
    ) -> bool:
        """Store a token in Redis with TTL.

        Returns True if successful, False otherwise.
        """
        try:
            client = await self._get_client()
            key = self._make_key(deployment_id)

            # Calculate TTL in seconds
            now = datetime.now(timezone.utc)
            if expires_at <= now:
                logger.warning("Token already expired, not caching")
                return False

            ttl_seconds = int((expires_at - now).total_seconds())

            # Store token data as JSON with expires_at timestamp
            token_data = json.dumps(
                {
                    "token": token,
                    "expires_at": expires_at.isoformat(),
                }
            )

            # Set with TTL (Redis will automatically delete when TTL expires)
            await client.setex(key, ttl_seconds, token_data)

            logger.debug(
                f"Cached token for deployment {deployment_id}, expires in {ttl_seconds}s"
            )
            return True

        except redis.RedisError as e:
            logger.warning(f"Redis error setting token: {e}")
            return False

        except (ValueError, TypeError) as e:
            logger.warning(f"Error serializing token data: {e}")
            return False

        except Exception as e:
            logger.warning(f"Unexpected error caching token: {e}")
            return False

    async def delete_token(self, deployment_id: str) -> bool:
        """Delete a token from Redis.

        Returns True if successful, False otherwise.
        """
        try:
            client = await self._get_client()
            key = self._make_key(deployment_id)
            deleted = await client.delete(key)
            return deleted > 0

        except redis.RedisError as e:
            logger.warning(f"Redis error deleting token: {e}")
            return False

        except Exception as e:
            logger.warning(f"Unexpected error deleting token from cache: {e}")
            return False

    async def delete_deployment_tokens(self, deployment_id: str) -> int:
        """Delete all tokens for a specific deployment.

        Returns count of deleted tokens.
        """
        try:
            client = await self._get_client()
            key = self._make_key(deployment_id)
            deleted = await client.delete(key)
            return deleted

        except redis.RedisError as e:
            logger.warning(f"Redis error deleting deployment tokens: {e}")
            return 0

        except Exception as e:
            logger.warning(f"Unexpected error deleting deployment tokens: {e}")
            return 0

    async def close(self) -> None:
        """Close Redis connection."""
        if self._redis_client:
            await self._redis_client.aclose()
            self._redis_client = None
