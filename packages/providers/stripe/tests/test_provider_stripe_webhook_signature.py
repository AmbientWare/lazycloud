from __future__ import annotations

import hashlib
import hmac

import pytest
from provider_stripe import verify_signature
from shared.errors import InvalidInputError

_SECRET = "whsec_test_endpoint_secret"
_BODY = b'{"id":"evt_1","type":"invoice.paid","data":{"object":{"id":"in_1"}}}'
_NOW = 1_800_000_000


def _sign(payload: bytes, secret: str, timestamp: int) -> str:
    return hmac.new(
        secret.encode("utf-8"), f"{timestamp}.".encode() + payload, hashlib.sha256
    ).hexdigest()


def test_the_endpoint_believes_only_what_its_own_secret_signed() -> None:
    """The signature is the whole of the authorization on this endpoint.

    It is public, unauthenticated by design, and what it carries changes what a
    customer owes and whether their account can run anything. Anything that gets
    past this is an anonymous request to do both.
    """

    header = f"t={_NOW},v1={_sign(_BODY, _SECRET, _NOW)}"
    verify_signature(payload=_BODY, header=header, secret=_SECRET, now=_NOW)

    forged = f"t={_NOW},v1={_sign(_BODY, 'whsec_someone_elses_secret', _NOW)}"
    with pytest.raises(InvalidInputError):
        verify_signature(payload=_BODY, header=forged, secret=_SECRET, now=_NOW)

    # The signature covers the body: a delivery whose amounts were edited in
    # flight carries a digest that no longer describes it.
    tampered = _BODY.replace(b"in_1", b"in_9")
    with pytest.raises(InvalidInputError):
        verify_signature(payload=tampered, header=header, secret=_SECRET, now=_NOW)


def test_a_signature_that_was_valid_once_does_not_stay_valid() -> None:
    """A captured delivery replayed later would apply its outcome a second time.

    The digest alone cannot tell the two apart — a replay is a genuine signature
    over a genuine body — so age is the only thing that distinguishes them.
    """

    stale = _NOW - 3_600
    header = f"t={stale},v1={_sign(_BODY, _SECRET, stale)}"
    with pytest.raises(InvalidInputError):
        verify_signature(payload=_BODY, header=header, secret=_SECRET, now=_NOW)


def test_a_malformed_signature_is_refused_rather_than_raised_through() -> None:
    """Anything an anonymous caller sends must end as a refusal, never an error.

    Headers decode as latin-1, and `compare_digest` raises on a str holding
    anything outside ASCII — so one stray byte would leave the endpoint answering
    500 and logging a traceback per request, on a public route with no
    credential. That turns a terminal rejection into a retried failure and hands
    anyone a way to fill the log.
    """

    for header in (
        f"t={_NOW},v1=" + "\u00c3" * 64,
        f"t={_NOW},v1=",
        f"t={_NOW}",
        f"t=not-a-number,v1={_sign(_BODY, _SECRET, _NOW)}",
        f"v1={_sign(_BODY, _SECRET, _NOW)}",
        "",
        f"t={_NOW},v1=" + "z" * 64,
        f"t={_NOW},v1={_sign(_BODY, _SECRET, _NOW)[:32]}",
    ):
        with pytest.raises(InvalidInputError):
            verify_signature(payload=_BODY, header=header, secret=_SECRET, now=_NOW)


def test_a_delivery_signed_during_a_secret_rotation_is_still_accepted() -> None:
    """Stripe sends a digest per active secret while an endpoint is rotating.

    Reading only the first would reject every delivery for the length of a
    rotation, which is an outage in exactly the window an operator is least able
    to tell a broken endpoint from a broken secret.
    """

    header = (
        f"t={_NOW},v1={_sign(_BODY, 'whsec_the_old_one', _NOW)},v1={_sign(_BODY, _SECRET, _NOW)}"
    )
    verify_signature(payload=_BODY, header=header, secret=_SECRET, now=_NOW)
