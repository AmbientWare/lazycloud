from __future__ import annotations

from coordination.event_bus import (
    EVENT_LOCK_TTL_SECONDS,
    EVENT_RETRY_DELAY_SECONDS,
    EVENT_TTL_SECONDS,
    EventBusClaimStatus,
    EventBusEvent,
    EventBusEventType,
    EventBusHandleAction,
    EventBusResendStatus,
    EventBusSendStatus,
    RedisEventBus,
    event_channel_key,
    event_id_for_event,
    event_key,
    event_lock_key,
    event_retry_key,
    serialize_event_bus_event,
)
from coordination.redis_client import RedisClient
from shared.app_identity import REDIS_KEY_PREFIX
from tests.real_redis import RealRedisActors
from tests.redis_fakes import FakeRedis


def test_event_bus_send_uses_deterministic_ids_ttl_and_duplicate_guard() -> None:
    fake = FakeRedis()
    bus = RedisEventBus(RedisClient(fake))
    event = EventBusEvent(
        type=EventBusEventType.StopContainer,
        args={"container_id": "ctr", "force": True},
        lock_and_delete=True,
        retries=2,
    )

    first = bus.send(event)
    second = bus.send(event)

    assert event_id_for_event(event) == event_id_for_event(event.model_copy())
    assert serialize_event_bus_event(event).startswith('{"type":"STOP_CONTAINER"')
    assert first.status is EventBusSendStatus.Sent
    assert first.event_id == event_id_for_event(event)
    assert first.event_key == f"{REDIS_KEY_PREFIX}:{event_key(first.event_id)}"
    assert first.channel == f"{REDIS_KEY_PREFIX}:events/STOP_CONTAINER"
    assert fake.expirations[first.event_key] == EVENT_TTL_SECONDS
    assert fake.published == [(f"{REDIS_KEY_PREFIX}:events/STOP_CONTAINER", first.event_id)]
    assert second.status is EventBusSendStatus.Duplicate
    assert len(fake.published) == 1


def test_event_bus_claims_lock_and_successful_handler_deletes_event(
    real_redis_actors: RealRedisActors,
) -> None:
    redis = real_redis_actors.client()
    bus = RedisEventBus(redis)
    send = bus.send(
        EventBusEvent(
            type=EventBusEventType.StopBuild,
            args={"container_id": "build-1"},
            lock_and_delete=True,
        )
    )

    claim = bus.claim(send.event_id)
    assert claim.status is EventBusClaimStatus.Claimed
    assert claim.lock_key == redis.key(event_lock_key(send.event_id))
    lock_ttl = redis.ttl(claim.lock_key)
    assert 0 < lock_ttl <= EVENT_LOCK_TTL_SECONDS

    locked = bus.claim(send.event_id)
    handled = bus.handle_claimed(claim, lambda event: event.args["container_id"] == "build-1")

    assert locked.status is EventBusClaimStatus.Locked
    assert handled.action is EventBusHandleAction.Delete
    assert handled.deleted
    assert handled.lock_released
    assert redis.get(send.event_key) is None
    assert redis.get(claim.lock_key) is None


def test_event_bus_missing_callback_republishes_and_releases_lock_without_delete(
    real_redis_actors: RealRedisActors,
) -> None:
    redis = real_redis_actors.client()
    bus = RedisEventBus(redis)
    send = bus.send(
        EventBusEvent(
            type=EventBusEventType.ReloadInstance,
            args={"instance_id": "i-1"},
            lock_and_delete=True,
            retries=1,
        )
    )
    claim = bus.claim(send.event_id)
    subscriber = redis.pubsub()
    subscriber.subscribe(send.channel)
    subscription = subscriber.get_message(timeout=1.0)
    assert subscription is not None
    assert subscription.type == "subscribe"
    assert subscription.pattern is None

    try:
        handled = bus.handle_claimed(claim, None)
        republished = subscriber.get_message(ignore_subscribe_messages=True, timeout=1.0)
    finally:
        subscriber.close()

    assert handled.action is EventBusHandleAction.Republish
    assert handled.republished == 1
    assert republished is not None
    assert republished.data == send.event_id
    assert republished.pattern is None
    assert handled.lock_released
    assert redis.get(send.event_key) is not None
    assert redis.get(claim.lock_key) is None


def test_event_bus_failed_handler_resends_until_retry_limit() -> None:
    fake = FakeRedis()
    bus = RedisEventBus(RedisClient(fake))
    event = EventBusEvent(
        type=EventBusEventType.StopBuild,
        args={"container_id": "build-2"},
        lock_and_delete=False,
        retries=1,
    )
    send = bus.send(event)
    claim = bus.claim(send.event_id)
    fake.published.clear()

    first = bus.handle_claimed(claim, lambda event: False)
    second = bus.handle_claimed(claim, lambda event: False)

    assert event_channel_key(event.type) == "events/STOP_BUILD"
    assert first.action is EventBusHandleAction.Resend
    assert first.resend is not None
    assert first.resend.status is EventBusResendStatus.Published
    assert first.resend.retry_key == f"{REDIS_KEY_PREFIX}:{event_retry_key(send.event_id)}"
    assert first.resend.retry_count == 1
    assert first.resend.retry_delay_seconds == EVENT_RETRY_DELAY_SECONDS
    assert fake.expirations[first.resend.retry_key] == EVENT_TTL_SECONDS
    assert fake.published == [(f"{REDIS_KEY_PREFIX}:events/STOP_BUILD", send.event_id)]
    assert second.action is EventBusHandleAction.RetryLimit
    assert second.resend is not None
    assert second.resend.status is EventBusResendStatus.RetryLimit
    assert second.resend.retry_count == 1
    assert len(fake.published) == 1
