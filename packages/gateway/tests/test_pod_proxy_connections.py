from __future__ import annotations

import asyncio

import pytest
from coordination.redis_client import AsyncRedisClient, RedisSettings
from gateway.pod_proxy import (
    AsyncRedisPodProxyConnectionRepository,
)
from shared.workload_keys import (
    pod_container_connections_key,
    pod_keep_warm_lock_key,
    pod_total_connections_key,
)
from tests.real_redis import RealRedisActors


@pytest.mark.anyio
async def test_real_redis_connection_counters_are_atomic_and_leave_no_idle_keys(
    real_redis_actors: RealRedisActors,
) -> None:
    redis = _async_redis(real_redis_actors)
    repository = AsyncRedisPodProxyConnectionRepository(redis)
    operations = 64
    try:
        increments = await asyncio.gather(
            *(_increment_both(repository) for _index in range(operations))
        )
        assert sorted(container for container, _total in increments) == list(
            range(1, operations + 1)
        )
        assert sorted(total for _container, total in increments) == list(range(1, operations + 1))
        assert (
            await repository.container_connections("workspace", "stub", "container") == operations
        )

        decrements = await asyncio.gather(
            *(_decrement_both(repository) for _index in range(operations))
        )
        assert sorted(container for container, _total in decrements) == list(range(operations))
        assert sorted(total for _container, total in decrements) == list(range(operations))
        assert await repository.container_connections("workspace", "stub", "container") == 0
        assert not await redis.exists(_container_key(redis))
        assert not await redis.exists(_total_key(redis))

        assert (
            await repository.decrement_container_connections(
                "workspace",
                "stub",
                "container",
                keep_warm_seconds=0,
            )
            == 0
        )
        assert await repository.decrement_total_connections("workspace", "stub") == 0
    finally:
        await redis.close()


@pytest.mark.anyio
async def test_connection_open_only_persists_an_existing_keep_warm_lock(
    real_redis_actors: RealRedisActors,
) -> None:
    redis = _async_redis(real_redis_actors)
    repository = AsyncRedisPodProxyConnectionRepository(redis)
    lock_key = redis.key(pod_keep_warm_lock_key("workspace", "stub", "container"))
    try:
        await repository.increment_container_connections(
            "workspace", "stub", "container", keep_warm_seconds=30
        )
        assert not await redis.exists(lock_key)

        await redis.set(lock_key, "1", ex=1)
        await repository.increment_container_connections(
            "workspace", "stub", "container", keep_warm_seconds=30
        )
        assert await redis.ttl(lock_key) == -1

        await repository.decrement_container_connections(
            "workspace", "stub", "container", keep_warm_seconds=30
        )
        assert (
            await repository.decrement_container_connections(
                "workspace", "stub", "container", keep_warm_seconds=30
            )
            == 0
        )
        assert 0 < await redis.ttl(lock_key) <= 30
    finally:
        await redis.close()


async def _increment_both(
    repository: AsyncRedisPodProxyConnectionRepository,
) -> tuple[int, int]:
    return await asyncio.gather(
        repository.increment_container_connections(
            "workspace", "stub", "container", keep_warm_seconds=0
        ),
        repository.increment_total_connections("workspace", "stub"),
    )


async def _decrement_both(
    repository: AsyncRedisPodProxyConnectionRepository,
) -> tuple[int, int]:
    return await asyncio.gather(
        repository.decrement_container_connections(
            "workspace", "stub", "container", keep_warm_seconds=0
        ),
        repository.decrement_total_connections("workspace", "stub"),
    )


def _async_redis(real_redis_actors: RealRedisActors) -> AsyncRedisClient:
    return AsyncRedisClient.from_settings(
        RedisSettings(
            url=real_redis_actors.url,
            key_prefix=real_redis_actors.prefix,
            decode_responses=True,
            socket_timeout_seconds=2.0,
            health_check_interval_seconds=1,
        )
    )


def _container_key(redis: AsyncRedisClient) -> str:
    return redis.key(pod_container_connections_key("workspace", "stub", "container"))


def _total_key(redis: AsyncRedisClient) -> str:
    return redis.key(pod_total_connections_key("workspace", "stub"))
