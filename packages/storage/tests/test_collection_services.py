from __future__ import annotations

import pytest
from execution.collections.planning import MAX_MAP_VALUE_SIZE_BYTES
from execution.collections.redis import (
    RedisMapService,
)
from shared.errors import NotFoundError
from shared.http.collections import MAX_MAP_TTL_SECONDS
from tests.real_redis import RealRedisActors


def test_redis_map_service_sets_indexes_lists_live_keys_and_removes_stale_entries(
    real_redis_actors: RealRedisActors,
) -> None:
    redis = real_redis_actors.client()
    service = RedisMapService(redis)

    service.map_set("workspace", "cache", "b", b"two", ttl_seconds=60)
    service.map_set("workspace", "cache", "a", b"one")

    value_key = redis.key("map:workspace:cache:b")
    assert service.map_get("workspace", "cache", "b") == b"two"
    assert redis.ttl(value_key) == 60
    assert service.map_get("workspace", "cache", "a") == b"one"
    assert service.map_count("workspace", "cache") == 2
    assert service.map_keys("workspace", "cache") == ("a", "b")
    stats = service.map_stats("workspace", "cache")
    assert stats.count == 2
    assert stats.size_bytes == 6
    assert stats.expiring_keys == 1
    assert stats.nearest_expiry_seconds == 60

    redis.delete(redis.key("map:workspace:cache:a"))
    assert service.map_keys("workspace", "cache") == ("b",)
    assert redis.set_members(redis.key("map:workspace:cache:index")) == {"b"}

    with pytest.raises(NotFoundError, match="map key not found"):
        service.map_get("workspace", "cache", "a")
    service.map_delete("workspace", "cache", "b")
    assert service.map_count("workspace", "cache") == 0

    with pytest.raises(ValueError, match="map value exceeds"):
        service.map_set("workspace", "cache", "large", b"x" * (MAX_MAP_VALUE_SIZE_BYTES + 1))
    with pytest.raises(ValueError, match="map ttl exceeds"):
        service.map_set(
            "workspace",
            "cache",
            "long-lived",
            b"value",
            ttl_seconds=MAX_MAP_TTL_SECONDS + 1,
        )
