"""General-purpose Redis cache service."""

import json
from typing import Any

import redis.asyncio as redis
from loguru import logger

# Default TTL of 15 minutes
DEFAULT_TTL_SECONDS = 900


class CacheService:
    """General-purpose Redis cache with JSON serialization and TTL support."""

    def __init__(self, redis_url: str):
        self.redis_url = redis_url
        self._redis_client: redis.Redis | None = None

    async def _get_client(self) -> redis.Redis:
        """Get or create Redis client."""
        if self._redis_client is None:
            self._redis_client = redis.from_url(
                self.redis_url, decode_responses=True, encoding="utf-8"
            )
        return self._redis_client

    async def get(self, key: str) -> Any | None:
        """Get a value from cache.

        Returns None if key doesn't exist or on error.
        """
        try:
            client = await self._get_client()
            data = await client.get(key)
            if data is None:
                return None
            return json.loads(data)
        except redis.RedisError as e:
            logger.warning(f"Redis error getting key {key}: {e}")
            return None
        except json.JSONDecodeError as e:
            logger.warning(f"JSON decode error for key {key}: {e}")
            return None

    async def set(
        self, key: str, value: Any, ttl_seconds: int = DEFAULT_TTL_SECONDS
    ) -> bool:
        """Set a value in cache with TTL.

        Returns True if successful, False otherwise.
        """
        try:
            client = await self._get_client()
            data = json.dumps(value)
            await client.setex(key, ttl_seconds, data)
            return True
        except redis.RedisError as e:
            logger.warning(f"Redis error setting key {key}: {e}")
            return False
        except (TypeError, ValueError) as e:
            logger.warning(f"JSON encode error for key {key}: {e}")
            return False

    async def delete(self, key: str) -> bool:
        """Delete a key from cache.

        Returns True if key was deleted, False otherwise.
        """
        try:
            client = await self._get_client()
            deleted = await client.delete(key)
            return deleted > 0
        except redis.RedisError as e:
            logger.warning(f"Redis error deleting key {key}: {e}")
            return False

    async def close(self) -> None:
        """Close Redis connection."""
        if self._redis_client:
            await self._redis_client.aclose()
            self._redis_client = None
