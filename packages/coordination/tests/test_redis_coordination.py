from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import pytest
from coordination.event_bus import EventBusEvent, EventBusEventType, RedisEventBus
from coordination.redis_client import (
    RedisClient,
    RedisSettings,
)
from coordination.request_cooldown import RedisRequestCooldown
from coordination.token_lock import (
    TokenLockReleaseStatus,
    release_token_lock,
    try_acquire_token_lock,
)
from coordination.wake_signal import RedisWakeSignal
from tests.real_redis import RealRedisActors


def test_request_cooldown_keeps_longest_cross_replica_deadline(
    real_redis_actors: RealRedisActors,
) -> None:
    first = RedisRequestCooldown(real_redis_actors.client(), "provider-project")
    second = RedisRequestCooldown(real_redis_actors.client(), "provider-project")
    later = datetime.now(UTC).replace(microsecond=0) + timedelta(minutes=5)
    earlier = later - timedelta(minutes=1)
    with ThreadPoolExecutor(max_workers=2) as executor:
        updates = [
            executor.submit(first.defer_until, later),
            executor.submit(second.defer_until, earlier),
        ]
        for update in updates:
            update.result()
    assert first.blocked_until() == second.blocked_until() == later
    assert second.defer_until(earlier) == later
    assert first.redis.ttl(first.key) > 0
    assert RedisRequestCooldown(first.redis, "other-project").blocked_until() is None


def test_redis_settings_reject_implicit_query_knobs_and_invalid_urls() -> None:
    with pytest.raises(ValueError, match="query options"):
        RedisClient.from_settings(RedisSettings(url="redis://redis.internal/0?socket_timeout=1"))
    with pytest.raises(ValueError, match="scheme"):
        RedisClient.from_settings(RedisSettings(url="http://redis.internal/0"))
    with pytest.raises(ValueError, match="database"):
        RedisClient.from_settings(RedisSettings(url="redis://redis.internal/not-a-db"))


def test_token_lock_rejects_empty_tokens_and_non_positive_ttl(
    real_redis_actors: RealRedisActors,
) -> None:
    redis = real_redis_actors.client()
    with pytest.raises(ValueError, match="TTL"):
        try_acquire_token_lock(redis, "lock", "owner", ttl_seconds=0)
    with pytest.raises(ValueError, match="token is required"):
        try_acquire_token_lock(redis, "lock", "", ttl_seconds=10)
    with pytest.raises(ValueError, match="token is required"):
        release_token_lock(redis, "lock", "")


def test_real_redis_scripts_locks_and_pubsub_leave_no_keys(
    real_redis_actors: RealRedisActors,
) -> None:
    lock_owner = RedisClient.from_settings(
        RedisSettings(
            url=real_redis_actors.url,
            key_prefix=real_redis_actors.prefix,
            client_name="Coordination Acceptance #1!",
            socket_timeout_seconds=2.0,
            health_check_interval_seconds=1,
        )
    )
    assert lock_owner.ping()
    real_redis_actors.clients.append(lock_owner)
    replacement = real_redis_actors.client()
    lock_key = lock_owner.key("coordination", "replacement-lock")
    assert try_acquire_token_lock(lock_owner, lock_key, "expired-owner", ttl_seconds=30)
    replacement.set(lock_key, "replacement", ex=30)
    assert (
        release_token_lock(lock_owner, lock_key, "expired-owner")
        is TokenLockReleaseStatus.TokenMismatch
    )
    assert replacement.get(lock_key) == "replacement"
    assert (
        release_token_lock(replacement, lock_key, "replacement") is TokenLockReleaseStatus.Released
    )

    scope = "wake-script"
    wake_signals = [
        RedisWakeSignal(real_redis_actors.client(), scope=scope) for _index in range(16)
    ]
    with ThreadPoolExecutor(max_workers=len(wake_signals)) as executor:
        queued = list(
            executor.map(
                _signal_wake,
                [(wake, index) for index, wake in enumerate(wake_signals)],
            )
        )
    assert queued.count(True) == 1
    assert wake_signals[0].wait(timeout_seconds=0.5)
    assert not wake_signals[0].wait(timeout_seconds=0.05)

    publisher = real_redis_actors.client()
    subscriber_client = real_redis_actors.client()
    bus = RedisEventBus(publisher)
    event = EventBusEvent(
        type=EventBusEventType.StopBuild,
        args={"container_id": "build-real"},
        lock_and_delete=True,
    )
    channel = publisher.key("events/STOP_BUILD")
    subscriber = subscriber_client.pubsub(ignore_subscribe_messages=False)
    try:
        subscriber.subscribe(channel)
        subscription = subscriber.get_message(timeout=2.0)
        assert subscription is not None
        assert subscription.type == "subscribe"
        assert subscription.pattern is None
        assert subscription.channel == channel
        sent = bus.send(event)
        message = subscriber.get_message(timeout=2.0)
        assert message is not None
        assert message.pattern is None
        assert message.channel == channel
        assert message.data == sent.event_id

        claim = bus.claim(sent.event_id)
        assert claim.lock_token
        replacement.set(claim.lock_key, "replacement-event-owner", ex=30)
        handled = bus.handle_claimed(claim, lambda _event: True)
        assert not handled.lock_released
        assert replacement.get(claim.lock_key) == "replacement-event-owner"
        assert (
            release_token_lock(
                replacement,
                claim.lock_key,
                "replacement-event-owner",
            )
            is TokenLockReleaseStatus.Released
        )
    finally:
        subscriber.close()


def _signal_wake(item: tuple[RedisWakeSignal, int]) -> bool:
    wake, _index = item
    return wake.signal()
