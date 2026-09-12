from __future__ import annotations

from dataclasses import dataclass

import httpx
from coordination.redis_client import AsyncRedisClient, RedisSettings
from coordination.stream_tail import RedisStreamTailBroker
from identity.token_invalidation import AsyncAuthTokenInvalidation

from api.server.worker_event_broker import WorkerEventBroker
from database import AsyncDatabaseClient, DatabaseSettings


@dataclass(frozen=True, slots=True)
class ApiAsyncIo:
    database: AsyncDatabaseClient
    redis: AsyncRedisClient
    binary_redis: AsyncRedisClient
    auth_invalidation: AsyncAuthTokenInvalidation
    worker_events: WorkerEventBroker
    realtime: RedisStreamTailBroker
    object_upload_http: httpx.AsyncClient

    @classmethod
    def from_settings(
        cls,
        database_settings: DatabaseSettings,
        redis_settings: RedisSettings,
    ) -> ApiAsyncIo:
        database = AsyncDatabaseClient.from_settings(database_settings)
        redis = AsyncRedisClient.from_settings(redis_settings)
        binary_redis = AsyncRedisClient.from_settings(redis_settings, decode_responses=False)
        return cls(
            database=database,
            redis=redis,
            binary_redis=binary_redis,
            auth_invalidation=AsyncAuthTokenInvalidation.from_redis(redis),
            worker_events=WorkerEventBroker(redis),
            realtime=RedisStreamTailBroker(redis),
            object_upload_http=httpx.AsyncClient(trust_env=False, follow_redirects=False),
        )

    async def start(self) -> None:
        await self.worker_events.start()
        await self.realtime.start()

    async def close(self) -> None:
        failures: list[BaseException] = []
        for close in (
            self.object_upload_http.aclose,
            self.realtime.close,
            self.worker_events.close,
            self.binary_redis.close,
            self.redis.close,
            self.database.dispose,
        ):
            try:
                await close()
            except BaseException as exc:
                failures.append(exc)
        if failures:
            raise BaseExceptionGroup("asynchronous API I/O shutdown was incomplete", failures)


__all__ = ["ApiAsyncIo"]
