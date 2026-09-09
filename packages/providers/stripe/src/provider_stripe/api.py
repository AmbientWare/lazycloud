from __future__ import annotations

from collections.abc import Sequence
from typing import Any
from urllib.parse import urlencode

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from shared.errors import InvalidInputError, UpstreamUnavailableError

API_BASE_URL = "https://api.stripe.com/v1"

API_VERSION = "2026-07-29.dahlia"
"""The shape of Stripe's answers this package was written against.

Sent on every request. Unpinned, the shapes below are whatever the account's
dashboard default happens to be, which is a setting nobody here can see and which
Stripe advances on their schedule — so a field this package reads could be
renamed by an upgrade nobody in this repository made, and `extra="ignore"` would
let the response validate right up until the field it needed was the one missing.

This is the version the account already serves, so pinning it changes nothing
today. What it changes is later: a Stripe release becomes a value edited here,
with the live billing scenarios run against it, rather than a shape that moved
underneath a running deployment.

It has a floor as well as a ceiling. The models read a subscription's billing
period from its *items* and an invoice line's price from `pricing.price_details`,
both of which arrived in `2025-03-31.basil`, and `/invoices/create_preview`
exists only from that release — the endpoint it replaced now answers 404. Pinning
below basil would not hold this package still, it would break it.
"""

type FormFields = Sequence[tuple[str, str]]
"""Form pairs rather than a mapping.

Stripe's encoding repeats a key to express a list — `lookup_keys[]` twice is two
lookup keys — and a mapping cannot hold the second one.
"""


class StripeObject(BaseModel):
    """Base for the parts of a Stripe object this platform reads.

    `extra="ignore"` is the opposite of this repository's own contracts and is
    deliberate: Stripe owns these shapes and versions them on their schedule, so
    a response carrying a field added last Tuesday is normal traffic. Naming only
    what is read keeps this package from becoming a second description of their
    API.
    """

    model_config = ConfigDict(extra="ignore")


class _ErrorDetail(StripeObject):
    message: str = ""


class _ErrorBody(StripeObject):
    error: _ErrorDetail = Field(default_factory=_ErrorDetail)


def build_client(*, api_key: str, timeout_seconds: float = 30.0) -> httpx.Client:
    return httpx.Client(
        base_url=API_BASE_URL,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/x-www-form-urlencoded",
            # On the client rather than per call, so every request carries it —
            # including the ones that add an idempotency key, and including the
            # catalog publisher, which builds its client through here too.
            "Stripe-Version": API_VERSION,
        },
        timeout=timeout_seconds,
    )


def send(
    client: httpx.Client,
    method: str,
    path: str,
    *,
    data: FormFields = (),
    params: FormFields = (),
    idempotency_key: str = "",
) -> None:
    """Make a call whose only outcome is whether Stripe accepted it."""

    _call(client, method, path, data=data, params=params, idempotency_key=idempotency_key)


def read[T: StripeObject](
    model: type[T],
    client: httpx.Client,
    method: str,
    path: str,
    *,
    data: FormFields = (),
    params: FormFields = (),
    idempotency_key: str = "",
) -> T:
    """Make a call and validate the answer into the shape the caller reads.

    Every call site names the model it expects, so no untyped body escapes this
    module and no caller reaches into a response by string key.
    """

    body = _call(client, method, path, data=data, params=params, idempotency_key=idempotency_key)
    try:
        return model.model_validate(body)
    except ValidationError as exc:
        # Retryable, because nothing about the request is wrong: either Stripe
        # answered with something other than the documented object, or the
        # account is mid-change. Terminal here would abandon a charge over a
        # shape this platform cannot influence.
        raise UpstreamUnavailableError(
            f"Stripe answered {path} without what this platform reads: {exc.error_count()} problems"
        ) from None


def _call(
    client: httpx.Client,
    method: str,
    path: str,
    *,
    data: FormFields,
    params: FormFields,
    idempotency_key: str = "",
) -> Any:
    # Stripe remembers a keyed request for 24 hours and answers a repeat of it
    # with what the first attempt produced rather than doing the work again. Sent
    # only where the caller named a key, because a key is a claim that two calls
    # are the same call and only the caller knows when they are.
    headers = {"Idempotency-Key": idempotency_key} if idempotency_key else None
    try:
        # Encoded here rather than handed to the client as a mapping: Stripe
        # repeats a key to express a list, and the order of `items[0]`,
        # `items[1]` is the order of the subscription's lines.
        response = client.request(
            method,
            path,
            params=list(params),
            content=urlencode(list(data)).encode(),
            headers=headers,
        )
    except httpx.RequestError as exc:
        raise UpstreamUnavailableError(f"Stripe request failed: {exc!s}") from exc
    try:
        body = response.json()
    except ValueError as exc:
        raise UpstreamUnavailableError(
            f"Stripe returned a non-JSON response ({response.status_code})"
        ) from exc
    if response.status_code >= httpx.codes.BAD_REQUEST:
        _raise_api_error(body, status_code=response.status_code)
    return body


def _raise_api_error(body: Any, *, status_code: int) -> None:
    try:
        message = _ErrorBody.model_validate(body).error.message
    except ValidationError:
        message = ""
    message = message or f"Stripe rejected the request ({status_code})"
    # A rejected request is ours to fix; anything else is worth retrying. Two 4xx
    # codes are not the caller's fault and must not be terminal: a rate limit is
    # transient, and a rejected credential is an operator problem a rotation
    # fixes. Treating either as terminal would leave a customer unable to save a
    # card for reasons they cannot act on, and would abandon a meter event that
    # a re-issued key would have carried.
    if status_code in {
        httpx.codes.TOO_MANY_REQUESTS,
        httpx.codes.UNAUTHORIZED,
        httpx.codes.FORBIDDEN,
    }:
        raise UpstreamUnavailableError(message)
    if httpx.codes.BAD_REQUEST <= status_code < httpx.codes.INTERNAL_SERVER_ERROR:
        raise InvalidInputError(message)
    raise UpstreamUnavailableError(message)


__all__ = ["API_BASE_URL", "FormFields", "StripeObject", "build_client", "read", "send"]
