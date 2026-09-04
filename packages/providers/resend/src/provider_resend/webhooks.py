from __future__ import annotations

import base64
import hashlib
import hmac
from dataclasses import dataclass
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from shared.email import EmailDeliveryState
from shared.errors import InvalidInputError
from shared.timestamps import to_utc

ID_HEADER = "svix-id"
TIMESTAMP_HEADER = "svix-timestamp"
SIGNATURE_HEADER = "svix-signature"

TOLERANCE_SECONDS = 300
"""How far a delivery's own timestamp may be from ours.

Signed payloads replay perfectly, so the timestamp is what stops one being
resent forever. Five minutes is the window Svix documents and the one their
senders retry within.
"""

_SECRET_PREFIX = "whsec_"

# What the platform does about each. Anything else is a shape Resend owns and
# this does not read: an event nobody acts on is noise that still has to be
# parsed, and parsing it is how a rename becomes an outage.
_STATES: dict[str, EmailDeliveryState] = {
    "email.sent": EmailDeliveryState.Sent,
    "email.delivered": EmailDeliveryState.Delivered,
    "email.bounced": EmailDeliveryState.Bounced,
    "email.complained": EmailDeliveryState.Complained,
}


@dataclass(frozen=True, slots=True)
class EmailDeliveryEvent:
    """One thing a provider says became of a message it took earlier."""

    provider_message_id: str
    state: EmailDeliveryState
    occurred_at: datetime
    detail: str = ""


class _Bounce(BaseModel):
    model_config = ConfigDict(extra="ignore")

    message: str = ""
    subType: str = ""


class _Data(BaseModel):
    model_config = ConfigDict(extra="ignore")

    email_id: str = ""
    bounce: _Bounce = Field(default_factory=_Bounce)


class _Delivery(BaseModel):
    """The part of a Resend delivery this platform reads.

    `extra="ignore"` unlike this repository's own contracts, deliberately:
    Resend owns this shape and adds to it on their schedule, so a payload
    carrying a field added last week is ordinary traffic rather than a fault.
    """

    model_config = ConfigDict(extra="ignore")

    type: str = ""
    created_at: datetime | None = None
    data: _Data = Field(default_factory=_Data)


def verify_signature(
    *,
    payload: bytes,
    message_id: str,
    timestamp: str,
    signature_header: str,
    secret: str,
    now: int,
) -> None:
    """Refuse anything this endpoint cannot prove came from the provider.

    The URL is public and what arrives on it decides what an administrator is
    told about their invitations, so an unsigned body is somebody else's opinion.
    Compared with `compare_digest`, because a comparison that returns early
    leaks, one byte at a time, what the right answer was.
    """
    try:
        sent_at = int(timestamp)
    except ValueError as exc:
        raise InvalidInputError(f"{TIMESTAMP_HEADER} is not a unix timestamp") from exc
    if abs(now - sent_at) > TOLERANCE_SECONDS:
        raise InvalidInputError("delivery timestamp is outside the accepted window")

    signed = b".".join((message_id.encode(), timestamp.encode(), payload))
    expected = base64.b64encode(hmac.new(_key(secret), signed, hashlib.sha256).digest()).decode()
    # The header carries space-separated `v1,<signature>` pairs, because a
    # secret being rotated means two are briefly valid at once.
    for candidate in signature_header.split():
        version, _, value = candidate.partition(",")
        if version == "v1" and hmac.compare_digest(value, expected):
            return
    raise InvalidInputError("delivery signature does not match")


def parse_event(payload: bytes) -> EmailDeliveryEvent | None:
    """The delivery as something this platform acts on, or None where it is not.

    None rather than an error for an event we do not read. Resend sends kinds
    this platform has no opinion about, and answering those with a failure would
    have the provider retry, and eventually disable the endpoint, over messages
    that were never a problem.
    """
    try:
        delivery = _Delivery.model_validate_json(payload)
    except ValidationError as exc:
        raise InvalidInputError("delivery is not a readable Resend event") from exc
    state = _STATES.get(delivery.type)
    if state is None or not delivery.data.email_id or delivery.created_at is None:
        return None
    return EmailDeliveryEvent(
        provider_message_id=delivery.data.email_id,
        state=state,
        occurred_at=to_utc(delivery.created_at),
        detail=" ".join(
            part for part in (delivery.data.bounce.subType, delivery.data.bounce.message) if part
        ),
    )


def _key(secret: str) -> bytes:
    """The signing key, which is base64 after the prefix Svix writes on it."""
    raw = secret.removeprefix(_SECRET_PREFIX)
    try:
        return base64.b64decode(raw, validate=True)
    except ValueError as exc:
        raise InvalidInputError("the configured webhook secret is not valid base64") from exc


__all__ = [
    "ID_HEADER",
    "SIGNATURE_HEADER",
    "TIMESTAMP_HEADER",
    "TOLERANCE_SECONDS",
    "EmailDeliveryEvent",
    "parse_event",
    "verify_signature",
]
