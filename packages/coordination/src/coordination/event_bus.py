from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import dataclass
from enum import StrEnum

from pydantic import Field, JsonValue
from shared.contracts import ContractModel

from coordination.redis_client import RedisClient

EVENT_PREFIX = "event"
EVENT_CHANNEL_PREFIX = "events"
EVENT_TTL_SECONDS = 60


class EventBusEventType(StrEnum):
    StopContainer = "STOP_CONTAINER"
    StopBuild = "STOP_BUILD"
    ReloadInstance = "RELOAD_INSTANCE"
    PurgeSourceCache = "PURGE_SOURCE_CACHE"


class EventBusSendStatus(StrEnum):
    Sent = "sent"
    Duplicate = "duplicate"


class EventBusEvent(ContractModel):
    type: str
    args: dict[str, JsonValue] = Field(default_factory=dict)
    lock_and_delete: bool = False
    retries: int = Field(default=0, ge=0)


class EventBusSendResult(ContractModel):
    status: EventBusSendStatus
    event_id: str
    event_key: str
    channel: str
    published: int = 0
    reason: str = ""


def event_channel_key(event_type: str) -> str:
    return f"{EVENT_CHANNEL_PREFIX}/{event_type}"


def event_key(event_id: str) -> str:
    return f"{EVENT_PREFIX}:{event_id}"


def serialize_event_bus_event(event: EventBusEvent) -> str:
    payload: dict[str, JsonValue] = {
        "type": event.type,
        "args": {key: event.args[key] for key in sorted(event.args)},
        "lock_and_delete": event.lock_and_delete,
        "retries": event.retries,
    }
    return json.dumps(payload, separators=(",", ":"), sort_keys=False)


def event_id_for_event(event: EventBusEvent) -> str:
    digest = hashlib.sha1(serialize_event_bus_event(event).encode()).digest()
    return base64.urlsafe_b64encode(digest).decode()


@dataclass(slots=True)
class RedisEventBus:
    redis: RedisClient

    def send(self, event: EventBusEvent) -> EventBusSendResult:
        event_id = event_id_for_event(event)
        logical_key = event_key(event_id)
        key = self._key(logical_key)
        logical_channel = event_channel_key(event.type)
        channel = self._key(logical_channel)
        if self.redis.exists(key):
            return EventBusSendResult(
                status=EventBusSendStatus.Duplicate,
                event_id=event_id,
                event_key=key,
                channel=channel,
                reason=f"event already exists: {logical_key}",
            )

        self.redis.set(key, serialize_event_bus_event(event), ex=EVENT_TTL_SECONDS)
        published = self.redis.publish(channel, event_id)
        return EventBusSendResult(
            status=EventBusSendStatus.Sent,
            event_id=event_id,
            event_key=key,
            channel=channel,
            published=published,
            reason="event sent",
        )

    def delete(self, event_id: str) -> bool:
        return self.redis.delete(self._key(event_key(event_id))) > 0

    def _key(self, logical_key: str) -> str:
        return self.redis.key(logical_key)
