from __future__ import annotations

import hashlib
import hmac

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from shared.errors import InvalidInputError
from shared.payments import PaymentEvent

SIGNATURE_HEADER = "Stripe-Signature"

_HEX = frozenset("0123456789abcdefABCDEF")
_DIGEST_LENGTH = 64

DEFAULT_TOLERANCE_SECONDS = 300
"""How far out of date a delivery may be and still be believed.

Stripe's own default. A signature stays valid forever on its own — the timestamp
is inside the signed string precisely so a recipient can decide how old is too
old, and a recipient that does not decide has accepted that a copy of one
delivery can be replayed at any point in the future.
"""


def verify_signature(
    *,
    payload: bytes,
    header: str,
    secret: str,
    now: int,
    tolerance_seconds: int = DEFAULT_TOLERANCE_SECONDS,
) -> None:
    """Refuse a delivery this platform's endpoint secret did not sign.

    The endpoint is public and the events it carries move money and standing, so
    the signature is the whole of the authorization: anything that fails here is
    an anonymous request to change what a customer owes.

    Raises rather than returning a boolean, because a caller that forgot to check
    a returned `False` would have a working endpoint that trusts everyone, and
    nothing about the call site would look wrong.
    """

    timestamp, signatures = _parse_header(header)
    if abs(now - timestamp) > tolerance_seconds:
        # Age is checked before the digest, and separately from it: a valid
        # signature over an old payload is exactly what a replay is, and the two
        # failures are different enough that an operator needs to tell them
        # apart.
        raise InvalidInputError("stripe signature timestamp is outside the tolerance window")
    expected = hmac.new(
        secret.encode("utf-8"),
        f"{timestamp}.".encode() + payload,
        hashlib.sha256,
    ).hexdigest()
    # Every candidate is compared, and each comparison is constant-time. Stripe
    # sends more than one `v1` while an endpoint secret is being rotated, so
    # taking only the first would fail every delivery mid-rotation.
    if not any(hmac.compare_digest(expected, candidate) for candidate in signatures):
        raise InvalidInputError("stripe signature does not match the endpoint secret")


def _parse_header(header: str) -> tuple[int, tuple[str, ...]]:
    """Pull the timestamp and the candidate digests out of `t=...,v1=...`.

    Unknown schemes are skipped rather than refused. `v0` already exists and is
    not ours to verify, and Stripe adding another must not turn every delivery
    into a rejection.
    """

    timestamp: int | None = None
    signatures: list[str] = []
    for part in header.split(","):
        name, _, value = part.strip().partition("=")
        if name == "t":
            try:
                timestamp = int(value)
            except ValueError as exc:
                raise InvalidInputError("stripe signature timestamp is not a number") from exc
        elif name == "v1" and _is_digest(value):
            # Filtered here rather than at the comparison: `compare_digest` raises
            # on a str holding anything outside ASCII, and headers decode as
            # latin-1, so one stray byte from an anonymous caller would be an
            # unhandled error on a public endpoint rather than a refusal.
            signatures.append(value)
    if timestamp is None or not signatures:
        raise InvalidInputError("stripe signature header is missing a timestamp or a signature")
    return timestamp, tuple(signatures)


def _is_digest(value: str) -> bool:
    return len(value) == _DIGEST_LENGTH and all(char in _HEX for char in value)


class _EventObject(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str = ""
    customer: str | None = None
    payment_method: str | None = None
    payment_intent: str | None = None
    metadata: dict[str, str] = Field(default_factory=dict)


class _EventData(BaseModel):
    model_config = ConfigDict(extra="ignore")

    object: _EventObject


class _Event(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str
    type: str
    data: _EventData


def parse_event(body: bytes) -> PaymentEvent:
    """Read the few things a delivery has to say, refusing one that says none.

    Extra fields are ignored rather than forbidden — the opposite of this
    platform's own contracts, and deliberately. Stripe owns this shape and
    versions it on their schedule, so a delivery carrying a field added last
    Tuesday is normal traffic and rejecting it would stop payment outcomes
    arriving. Naming only what is read keeps this from becoming a second
    description of their API.

    A body that cannot be read at all is a different matter: it is either not
    from Stripe or not what Stripe documents, and both are worth failing on
    rather than acknowledging.
    """

    try:
        event = _Event.model_validate_json(body)
    except ValidationError as exc:
        raise InvalidInputError(
            f"stripe event is not readable: {exc.error_count()} problems"
        ) from None
    return PaymentEvent(
        id=event.id,
        type=event.type,
        object_id=event.data.object.id,
        # A customer event carries no `customer` field, because the object *is*
        # the customer.
        customer_id=event.data.object.customer
        or (event.data.object.id if event.type.startswith("customer.") else ""),
        payment_method_id=event.data.object.payment_method
        or (event.data.object.id if event.type.startswith("payment_method.") else ""),
        payment_id=event.data.object.payment_intent
        or (event.data.object.id if event.type.startswith("payment_intent.") else ""),
        credit_purchase_id=event.data.object.metadata.get("credit_purchase_id", ""),
    )


__all__ = [
    "DEFAULT_TOLERANCE_SECONDS",
    "SIGNATURE_HEADER",
    "parse_event",
    "verify_signature",
]
