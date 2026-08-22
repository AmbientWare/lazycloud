from __future__ import annotations

import base64
import hashlib
import json
import secrets
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum

from pydantic import Field, JsonValue
from shared.contracts import ContractModel

from coordination.redis_client import RedisClient, redis_text
from coordination.token_lock import (
    TokenLockReleaseStatus,
    release_token_lock,
    try_acquire_token_lock,
)

EVENT_PREFIX = "event"
EVENT_CHANNEL_PREFIX = "events"
EVENT_LOCK_SUFFIX = "lock"
EVENT_RETRIES_SUFFIX = "retries"
EVENT_LOCK_TTL_SECONDS = 60
EVENT_TTL_SECONDS = 60
EVENT_RETRY_DELAY_SECONDS = 5


class EventBusEventType(StrEnum):
    StopContainer = "STOP_CONTAINER"
    StopBuild = "STOP_BUILD"
    ReloadInstance = "RELOAD_INSTANCE"
    PurgeSourceCache = "PURGE_SOURCE_CACHE"


class EventBusSendStatus(StrEnum):
    Sent = "sent"
    Duplicate = "duplicate"


class EventBusClaimStatus(StrEnum):
    Claimed = "claimed"
    Missing = "missing"
    Invalid = "invalid"
    Locked = "locked"


class EventBusResendStatus(StrEnum):
    Published = "published"
    RetryLimit = "retry-limit"


class EventBusHandleAction(StrEnum):
    Complete = "complete"
    Delete = "delete"
    Republish = "republish"
    Resend = "resend"
    RetryLimit = "retry-limit"


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


class EventBusClaimResult(ContractModel):
    status: EventBusClaimStatus
    event_id: str
    event: EventBusEvent | None = None
    event_key: str = ""
    lock_key: str = ""
    lock_token: str = Field(default="", exclude=True, repr=False)
    reason: str = ""

    @property
    def claimed(self) -> bool:
        return self.status is EventBusClaimStatus.Claimed


class EventBusResendResult(ContractModel):
    status: EventBusResendStatus
    event_id: str
    channel: str
    retry_key: str
    retry_count: int = 0
    retry_limit: int = 0
    published: int = 0
    retry_delay_seconds: int = EVENT_RETRY_DELAY_SECONDS
    reason: str = ""


class EventBusHandleResult(ContractModel):
    action: EventBusHandleAction
    event_id: str
    channel: str
    deleted: bool = False
    republished: int = 0
    resend: EventBusResendResult | None = None
    lock_released: bool = False
    reason: str = ""


def event_channel_key(event_type: str) -> str:
    return f"{EVENT_CHANNEL_PREFIX}/{event_type}"


def event_key(event_id: str) -> str:
    return f"{EVENT_PREFIX}:{event_id}"


def event_lock_key(event_id: str) -> str:
    return f"{EVENT_PREFIX}:{event_id}:{EVENT_LOCK_SUFFIX}"


def event_retry_key(event_id: str) -> str:
    return f"{EVENT_PREFIX}:{event_id}:{EVENT_RETRIES_SUFFIX}"


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

    def claim(self, event_id: str) -> EventBusClaimResult:
        key = self._key(event_key(event_id))
        raw = self.redis.get(key)
        if raw is None:
            return EventBusClaimResult(
                status=EventBusClaimStatus.Missing,
                event_id=event_id,
                event_key=key,
                reason="event missing",
            )
        try:
            event = EventBusEvent.model_validate_json(redis_text(raw))
        except ValueError as exc:
            return EventBusClaimResult(
                status=EventBusClaimStatus.Invalid,
                event_id=event_id,
                event_key=key,
                reason=str(exc),
            )

        lock_key = ""
        lock_token = ""
        if event.lock_and_delete:
            lock_key = self._key(event_lock_key(event_id))
            lock_token = secrets.token_urlsafe(32)
            acquired = try_acquire_token_lock(
                self.redis,
                lock_key,
                lock_token,
                ttl_seconds=EVENT_LOCK_TTL_SECONDS,
            )
            if not acquired:
                return EventBusClaimResult(
                    status=EventBusClaimStatus.Locked,
                    event_id=event_id,
                    event=event,
                    event_key=key,
                    lock_key=lock_key,
                    reason="event lock not acquired",
                )

        return EventBusClaimResult(
            status=EventBusClaimStatus.Claimed,
            event_id=event_id,
            event=event,
            event_key=key,
            lock_key=lock_key,
            lock_token=lock_token,
            reason="event claimed",
        )

    def resend(self, event_id: str, event: EventBusEvent) -> EventBusResendResult:
        retry_key = self._key(event_retry_key(event_id))
        raw_current = self.redis.get(retry_key)
        current = int(redis_text(raw_current)) if raw_current is not None else 0
        channel = self._key(event_channel_key(event.type))
        if current >= event.retries:
            return EventBusResendResult(
                status=EventBusResendStatus.RetryLimit,
                event_id=event_id,
                channel=channel,
                retry_key=retry_key,
                retry_count=current,
                retry_limit=event.retries,
                reason="hit event retry limit",
            )

        retry_count = current + 1
        self.redis.set(retry_key, str(retry_count), ex=EVENT_TTL_SECONDS)
        published = self.redis.publish(channel, event_id)
        return EventBusResendResult(
            status=EventBusResendStatus.Published,
            event_id=event_id,
            channel=channel,
            retry_key=retry_key,
            retry_count=retry_count,
            retry_limit=event.retries,
            published=published,
            reason="event resent",
        )

    def handle_claimed(
        self,
        claim: EventBusClaimResult,
        callback: Callable[[EventBusEvent], bool] | None,
    ) -> EventBusHandleResult:
        if claim.event is None:
            return EventBusHandleResult(
                action=EventBusHandleAction.Complete,
                event_id=claim.event_id,
                channel="",
                reason="no event to handle",
            )

        event = claim.event
        channel = self._key(event_channel_key(event.type))
        result: EventBusHandleResult | None = None
        try:
            if callback is None:
                republished = self.redis.publish(channel, claim.event_id)
                result = EventBusHandleResult(
                    action=EventBusHandleAction.Republish,
                    event_id=claim.event_id,
                    channel=channel,
                    republished=republished,
                    reason="missing callback",
                )
                return result

            if callback(event):
                deleted = self.delete(claim.event_id) if event.lock_and_delete else False
                result = EventBusHandleResult(
                    action=(
                        EventBusHandleAction.Delete
                        if event.lock_and_delete
                        else EventBusHandleAction.Complete
                    ),
                    event_id=claim.event_id,
                    channel=channel,
                    deleted=deleted,
                    reason="event handled",
                )
                return result

            resend = self.resend(claim.event_id, event)
            result = EventBusHandleResult(
                action=(
                    EventBusHandleAction.Resend
                    if resend.status is EventBusResendStatus.Published
                    else EventBusHandleAction.RetryLimit
                ),
                event_id=claim.event_id,
                channel=channel,
                resend=resend,
                republished=resend.published,
                reason=resend.reason,
            )
            return result
        finally:
            if claim.lock_key and claim.lock_token:
                released = release_token_lock(
                    self.redis,
                    claim.lock_key,
                    claim.lock_token,
                )
                if result is not None:
                    result.lock_released = released is TokenLockReleaseStatus.Released

    def _key(self, logical_key: str) -> str:
        return self.redis.key(logical_key)
