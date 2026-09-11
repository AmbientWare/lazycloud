from __future__ import annotations

import asyncio

import pytest
from coordination.redis_client import AsyncRedisClient, redis_text
from coordination.stream_tail import RedisStreamTailBroker, RedisStreamTailSubscription
from tests.real_redis import RealRedisActors


@pytest.mark.anyio
async def test_real_redis_tail_replays_after_cursor_then_delivers_live_entries_once(
    real_redis_actors: RealRedisActors,
    async_redis: AsyncRedisClient,
) -> None:
    producer = real_redis_actors.client()
    broker = RedisStreamTailBroker(async_redis, block_milliseconds=100)
    await broker.start()
    stream = "tail/handoff"
    key = async_redis.key(stream)
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
    async_redis: AsyncRedisClient,
) -> None:
    producer = real_redis_actors.client()
    broker = RedisStreamTailBroker(async_redis, block_milliseconds=100, queue_size=2)
    await broker.start()
    stream = "tail/overflow"
    key = async_redis.key(stream)
    try:
        stalled = await broker.subscribe([stream], after={stream: None}, label="stalled")
        healthy = await broker.subscribe([stream], after={stream: None}, label="healthy")
        async with stalled, healthy:
            delivered: asyncio.Queue[str] = asyncio.Queue()
            async with asyncio.TaskGroup() as tasks:
                draining = tasks.create_task(_collect(healthy, 5, delivered=delivered))
                ids: list[str] = []
                for index in range(5):
                    ids.append(redis_text(producer.stream_add(key, {"n": str(index)})))
                    assert await asyncio.wait_for(delivered.get(), timeout=5) == ids[-1]
            assert draining.result() == ids
            assert broker.status().overflows == {}
            assert await asyncio.wait_for(_collect(stalled, 5), timeout=5) == ids
            assert broker.status().overflows == {"stalled": 1}
    finally:
        await broker.close()


@pytest.mark.anyio
async def test_new_stream_is_delivered_while_another_stream_is_idle(
    real_redis_actors: RealRedisActors,
    async_redis: AsyncRedisClient,
) -> None:
    producer = real_redis_actors.client()
    broker = RedisStreamTailBroker(async_redis, block_milliseconds=5_000)
    await broker.start()
    try:
        first = await broker.subscribe(["first"], after={"first": None}, label="first")
        async with first:
            producer.stream_add(async_redis.key("first"), {"value": "first"})
            items = first.items(heartbeat_seconds=10)
            try:
                assert await asyncio.wait_for(anext(items), timeout=1) is not None
                second = await broker.subscribe(["second"], after={"second": None}, label="second")
                async with second:
                    expected = redis_text(
                        producer.stream_add(async_redis.key("second"), {"value": "second"})
                    )
                    updates = second.items(heartbeat_seconds=10)
                    try:
                        item = await asyncio.wait_for(anext(updates), timeout=1)
                        assert item is not None
                        assert redis_text(item[1][0]) == expected
                    finally:
                        await updates.aclose()
            finally:
                await items.aclose()
    finally:
        await broker.close()


async def _collect(
    subscription: RedisStreamTailSubscription,
    count: int,
    *,
    delivered: asyncio.Queue[str] | None = None,
) -> list[str]:
    """`count` entry ids, then proof that the next thing is a heartbeat, not a duplicate."""
    ids: list[str] = []
    async for item in subscription.items(heartbeat_seconds=0.3):
        if item is None:
            if len(ids) >= count:
                return ids
            continue
        ids.append(redis_text(item[1][0]))
        if delivered is not None:
            delivered.put_nowait(ids[-1])
        assert len(ids) <= count, f"unexpected extra entry {ids[-1]}"
    return ids
