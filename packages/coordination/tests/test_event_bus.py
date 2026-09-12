from __future__ import annotations

from coordination.event_bus import (
    EVENT_TTL_SECONDS,
    EventBusEvent,
    EventBusEventType,
    EventBusSendStatus,
    RedisEventBus,
    event_id_for_event,
    event_key,
    serialize_event_bus_event,
)
from coordination.redis_client import RedisClient
from shared.app_identity import REDIS_KEY_PREFIX
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
