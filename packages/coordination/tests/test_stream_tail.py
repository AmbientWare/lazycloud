from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import pytest
from coordination.redis_client import AsyncRedisClient, RedisSettings, redis_text
from coordination.stream_tail import RedisStreamTailBroker, RedisStreamTailSubscription
from tests.real_redis import RealRedisActors


@pytest.fixture
async def redis(real_redis_actors: RealRedisActors) -> AsyncIterator[AsyncRedisClient]:
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


@pytest.mark.anyio
async def test_real_redis_tail_replays_after_cursor_then_delivers_live_entries_once(
    real_redis_actors: RealRedisActors,
    redis: AsyncRedisClient,
) -> None:
    producer = real_redis_actors.client()
    broker = RedisStreamTailBroker(redis, block_milliseconds=100)
    await broker.start()
    stream = "tail/handoff"
    key = redis.key(stream)
    try:
        ids = [redis_text(producer.stream_add(key, {"n": str(index)})) for index in range(3)]
        resuming = await broker.subscribe([stream], after={stream: ids[0]}, label="resume")
        live = await broker.subscribe([stream], after={stream: None}, label="live")
        ids.append(redis_text(producer.stream_add(key, {"n": "3"})))
        async with resuming, live:
            assert broker.status().subscribers == {"resume": 1, "live": 1}
            assert await asyncio.wait_for(_collect(resuming, 3), timeout=5) == ids[1:]
            assert await asyncio.wait_for(_collect(live, 1), timeout=5) == ids[3:]
        assert broker.status().subscribers == {"resume": 0, "live": 0}
        assert broker.status().sources == 0
    finally:
        await broker.close()


@pytest.mark.anyio
async def test_real_redis_tail_overflowed_subscriber_catches_up_without_delaying_others(
    real_redis_actors: RealRedisActors,
    redis: AsyncRedisClient,
) -> None:
    producer = real_redis_actors.client()
    broker = RedisStreamTailBroker(redis, block_milliseconds=100, queue_size=2)
    await broker.start()
    stream = "tail/overflow"
    key = redis.key(stream)
    try:
        stalled = await broker.subscribe([stream], after={stream: None}, label="stalled")
        healthy = await broker.subscribe([stream], after={stream: None}, label="healthy")
        async with stalled, healthy:
            draining = asyncio.create_task(_collect(healthy, 5))
            ids: list[str] = []
            for index in range(5):
                ids.append(redis_text(producer.stream_add(key, {"n": str(index)})))
                await asyncio.sleep(0.15)
            assert await asyncio.wait_for(draining, timeout=5) == ids
            assert broker.status().overflows == {}
            assert await asyncio.wait_for(_collect(stalled, 5), timeout=5) == ids
            assert broker.status().overflows == {"stalled": 1}
    finally:
        await broker.close()


async def _collect(subscription: RedisStreamTailSubscription, count: int) -> list[str]:
    """`count` entry ids, then proof that the next thing is a heartbeat, not a duplicate."""
    ids: list[str] = []
    async for item in subscription.items(heartbeat_seconds=0.3):
        if item is None:
            if len(ids) >= count:
                return ids
            continue
        ids.append(redis_text(item[1][0]))
        assert len(ids) <= count, f"unexpected extra entry {ids[-1]}"
    return ids
