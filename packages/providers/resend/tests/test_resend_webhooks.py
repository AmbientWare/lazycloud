from __future__ import annotations

import base64
import hashlib
import hmac
import json

import pytest
from provider_resend import parse_event, verify_signature
from shared.email import EmailDeliveryState
from shared.errors import InvalidInputError

_SECRET = "whsec_" + base64.b64encode(b"a signing secret of some length").decode()
_ID = "msg_2abc"


def _signed(payload: bytes, *, timestamp: str, secret: str = _SECRET) -> str:
    key = base64.b64decode(secret.removeprefix("whsec_"))
    signed = b".".join((_ID.encode(), timestamp.encode(), payload))
    return "v1," + base64.b64encode(hmac.new(key, signed, hashlib.sha256).digest()).decode()


def test_only_a_delivery_the_provider_signed_is_accepted() -> None:
    """The endpoint is a public URL, so the signature is what stands in for a token.

    Without this anybody could tell the platform that a colleague's invitation
    bounced, and an administrator would resend or withdraw a working offer on a
    stranger's say-so.
    """
    payload = json.dumps({"type": "email.delivered"}).encode()
    now = 1_800_000_000

    verify_signature(
        payload=payload,
        message_id=_ID,
        timestamp=str(now),
        signature_header=_signed(payload, timestamp=str(now)),
        secret=_SECRET,
        now=now,
    )

    forged = "whsec_" + base64.b64encode(b"a different secret entirely!!!").decode()
    with pytest.raises(InvalidInputError, match="signature does not match"):
        verify_signature(
            payload=payload,
            message_id=_ID,
            timestamp=str(now),
            signature_header=_signed(payload, timestamp=str(now), secret=forged),
            secret=_SECRET,
            now=now,
        )
    # A signed body replays perfectly, so its age is the only thing that stops one.
    with pytest.raises(InvalidInputError, match="outside the accepted window"):
        verify_signature(
            payload=payload,
            message_id=_ID,
            timestamp=str(now - 3600),
            signature_header=_signed(payload, timestamp=str(now - 3600)),
            secret=_SECRET,
            now=now,
        )


def test_a_bounce_is_read_and_an_unrelated_event_is_ignored() -> None:
    """An event nobody acts on answers None rather than failing.

    Refusing it would have Resend retry, and eventually disable the endpoint,
    over deliveries that were never a problem.
    """
    bounce = json.dumps(
        {
            "type": "email.bounced",
            "created_at": "2026-09-03T18:00:00.000Z",
            "data": {
                "email_id": "5a1f",
                "bounce": {"subType": "Suppressed", "message": "on the suppression list"},
            },
        }
    ).encode()

    event = parse_event(bounce)

    assert event is not None
    assert event.provider_message_id == "5a1f"
    assert event.state is EmailDeliveryState.Bounced
    assert "suppression list" in event.detail

    opened = json.dumps(
        {
            "type": "email.opened",
            "created_at": "2026-09-03T18:00:00.000Z",
            "data": {"email_id": "x"},
        }
    ).encode()
    assert parse_event(opened) is None
