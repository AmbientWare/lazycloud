from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from uuid import uuid4

import pytest
from coordination.redis_client import AsyncRedisClient, RedisSettings
from coordination.stream_tail import RedisStreamTailBroker

from tests.backing_services import redis_url
from tests.real_redis import RealRedisActors


@pytest.fixture
def real_redis_actors() -> Iterator[RealRedisActors]:
    actors = RealRedisActors(
        url=redis_url(),
        prefix=f"lazycloud:test:redis-acceptance:{uuid4()}",
    )
    actors.client()
    try:
        yield actors
    finally:
        actors.cleanup()


@pytest.fixture
async def async_redis(real_redis_actors: RealRedisActors) -> AsyncIterator[AsyncRedisClient]:
    client = AsyncRedisClient.from_settings(
        RedisSettings(
            url=real_redis_actors.url,
            key_prefix=real_redis_actors.prefix,
            socket_timeout_seconds=2.0,
            health_check_interval_seconds=1,
        )
    )
    try:
        yield client
    finally:
        await client.close()


@pytest.fixture
async def stream_broker(async_redis: AsyncRedisClient) -> AsyncIterator[RedisStreamTailBroker]:
    broker = RedisStreamTailBroker(async_redis, block_milliseconds=100)
    try:
        await broker.start()
        yield broker
    finally:
        await broker.close()
