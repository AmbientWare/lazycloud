from __future__ import annotations

from execution.collections.redis import (
    RedisMapService,
    RedisSimpleQueueService,
)
from tests.real_redis import RealRedisActors


def test_real_redis_map_round_trip_stats_and_workspace_cleanup(
    real_redis_actors: RealRedisActors,
) -> None:
    service = RedisMapService(real_redis_actors.client())

    service.map_set("workspace-a", "cache", "alpha", b"first", ttl_seconds=60)
    service.map_set("workspace-a", "cache", "beta", b"second")
    service.map_set("workspace-b", "cache", "other", b"preserved")

    assert service.map_get("workspace-a", "cache", "alpha") == b"first"
    assert service.map_keys("workspace-a", "cache") == ("alpha", "beta")
    assert service.map_names("workspace-a") == ("cache",)
    stats = service.map_stats("workspace-a", "cache")
    assert stats.count == 2
    assert stats.size_bytes == 11
    assert stats.expiring_keys == 1
    assert stats.nearest_expiry_seconds is not None
    assert 0 <= stats.nearest_expiry_seconds <= 60

    service.delete_workspace("workspace-a")

    assert service.map_names("workspace-a") == ()
    assert service.map_get("workspace-b", "cache", "other") == b"preserved"


def test_real_redis_simple_queue_round_trip_stats_and_workspace_cleanup(
    real_redis_actors: RealRedisActors,
) -> None:
    timestamps = iter((1_000.0, 1_010.0, 1_020.0, 1_030.0))
    service = RedisSimpleQueueService(real_redis_actors.client(), clock=lambda: next(timestamps))

    service.simple_queue_put("workspace-a", "jobs", b"first")
    service.simple_queue_put("workspace-a", "jobs", b"second")
    service.simple_queue_put("workspace-b", "jobs", b"preserved")

    assert service.simple_queue_names("workspace-a") == ("jobs",)
    assert service.simple_queue_size("workspace-a", "jobs") == 2
    assert service.simple_queue_peek("workspace-a", "jobs") == b"first"
    stats = service.simple_queue_stats("workspace-a", "jobs")
    assert stats.size == 2
    assert stats.oldest_message_age_seconds == 30.0
    assert stats.put_rate_per_minute == 2
    assert service.simple_queue_pop("workspace-a", "jobs") == b"first"

    service.delete_workspace("workspace-a")

    assert service.simple_queue_names("workspace-a") == ()
    assert service.simple_queue_empty("workspace-a", "jobs")
    assert service.simple_queue_peek("workspace-b", "jobs") == b"preserved"
