from __future__ import annotations

import pytest
from execution.collections.planning import MAX_MAP_VALUE_SIZE_BYTES
from execution.collections.redis import (
    RedisMapService,
    RedisSimpleQueueService,
)
from shared.errors import ConflictError, InvalidInputError, NotFoundError
from shared.http.collections import MAX_MAP_TTL_SECONDS
from tests.real_redis import RealRedisActors


def test_real_redis_map_round_trip_stats_and_workspace_cleanup(
    real_redis_actors: RealRedisActors,
) -> None:
    service = RedisMapService(real_redis_actors.client())

    service.map_set("workspace-a", "cache", "alpha", b"first", ttl_seconds=60)
    service.map_set("workspace-a", "cache", "beta", b"second")
    service.map_set("workspace-b", "cache", "other", b"preserved")

    assert service.map_get("workspace-a", "cache", "alpha").value == b"first"
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
    assert service.map_get("workspace-b", "cache", "other").value == b"preserved"


def test_real_redis_map_drops_expired_index_entries_and_refuses_oversized_entries(
    real_redis_actors: RealRedisActors,
) -> None:
    redis = real_redis_actors.client()
    service = RedisMapService(redis)

    service.map_set("workspace-a", "cache", "alpha", b"first")
    service.map_set("workspace-a", "cache", "beta", b"second")
    # An expired value leaves its index entry behind; reads must reconcile it
    # rather than report a key whose value is already gone.
    redis.delete(redis.key("map:workspace-a:cache:alpha"))

    assert service.map_keys("workspace-a", "cache") == ("beta",)
    assert service.map_count("workspace-a", "cache") == 1
    assert redis.set_members(redis.key("map:workspace-a:cache:index")) == {"beta"}
    with pytest.raises(NotFoundError, match="map key not found"):
        service.map_get("workspace-a", "cache", "alpha")

    with pytest.raises(InvalidInputError, match="larger than 1 MiB"):
        service.map_set(
            "workspace-a",
            "cache",
            "oversized",
            b"x" * (MAX_MAP_VALUE_SIZE_BYTES + 1),
        )
    with pytest.raises(InvalidInputError, match="longer than 1 week"):
        service.map_set(
            "workspace-a",
            "cache",
            "long-lived",
            b"value",
            ttl_seconds=MAX_MAP_TTL_SECONDS + 1,
        )
    assert service.map_keys("workspace-a", "cache") == ("beta",)


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


def test_map_conditional_edits_preserve_expiry_and_refuse_stale_writes(
    real_redis_actors: RealRedisActors,
) -> None:
    service = RedisMapService(real_redis_actors.client())
    other = RedisMapService(real_redis_actors.client())
    service.map_set("w", "cache", "key", b"first", ttl_seconds=60, if_absent=True)
    opened = service.map_get("w", "cache", "key")
    with pytest.raises(ConflictError):
        other.map_set("w", "cache", "key", b"duplicate", if_absent=True)
    other.map_set("w", "cache", "key", b"worker", ttl_seconds=120)
    for operation in ("save", "delete"):
        with pytest.raises(ConflictError):
            if operation == "save":
                service.map_set("w", "cache", "key", b"stale", if_revision=opened.revision)
            else:
                service.map_delete("w", "cache", "key", if_revision=opened.revision)
    current = service.map_get("w", "cache", "key")
    assert current.value == b"worker"
    service.map_set("w", "cache", "key", b"edited", ttl_seconds=None, if_revision=current.revision)
    saved = service.map_get("w", "cache", "key")
    assert saved.value == b"edited"
    assert saved.expires_at == current.expires_at
    other.map_set("w", "cache", "key", b"edited", ttl_seconds=0)
    with pytest.raises(ConflictError):
        service.map_set("w", "cache", "key", b"stale expiry", if_revision=saved.revision)
    persistent = service.map_get("w", "cache", "key")
    service.map_set(
        "w", "cache", "key", b"persistent", ttl_seconds=None, if_revision=persistent.revision
    )
    assert service.map_get("w", "cache", "key").expires_at is None
    service.map_delete("w", "cache", "key")
    with pytest.raises(ConflictError):
        service.map_set(
            "w", "cache", "key", b"resurrected", ttl_seconds=None, if_revision=persistent.revision
        )
    with pytest.raises(InvalidInputError, match="reserved"):
        service.map_set("w", "cache", "index", b"clobber index")


def test_map_key_pages_cover_live_keys_and_filter_by_literal_prefix(
    real_redis_actors: RealRedisActors,
) -> None:
    service = RedisMapService(real_redis_actors.client())
    expected = {f"job[*]-{number:04d}" for number in range(600)}
    for key in expected | {"other"}:
        service.map_set("w", "cache", key, b"value")
    service.map_set("other-workspace", "cache", "job[*]-foreign", b"private")
    cursor = None
    seen: set[str] = set()
    pages = 0
    while True:
        page = service.map_key_page("w", "cache", cursor=cursor, prefix="job[*]-", limit=50)
        seen.update(page.data)
        pages += 1
        cursor = page.next
        if cursor is None:
            break
    assert pages > 1
    assert seen == expected
