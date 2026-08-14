from __future__ import annotations

import json
from datetime import timedelta

import httpx
import pytest
from provider_stripe import API_BASE_URL, METER_EVENT_BACKFILL_DAYS, StripeBilling
from shared.errors import InvalidInputError, UpstreamUnavailableError
from shared.timestamps import utc_now

_NO_METER_BODY = {
    "error": {
        "type": "invalid_request_error",
        "message": (
            "No active meter found with event_name lazycloud_compute_cost_nanos. "
            "Create a meter with that event name to record usage against it."
        ),
    }
}


def _refusing_client() -> httpx.Client:
    """A provider that declines every meter event the way an unpublished account does."""

    def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(400, content=json.dumps(_NO_METER_BODY).encode())

    return httpx.Client(base_url=API_BASE_URL, transport=httpx.MockTransport(handler))


def _refusing_on_no_request() -> httpx.Client:
    """A provider that must not be reached at all."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"an aged-out event must not be offered: {request.url}")

    return httpx.Client(base_url=API_BASE_URL, transport=httpx.MockTransport(handler))


def test_an_unpublished_meter_is_waited_on_and_an_aged_out_event_is_given_up_on() -> None:
    """The split the outbox settles every row on, and the money that turns on it.

    The sweep abandons a row permanently on `InvalidInputError` and paces it for
    another attempt on anything else, so which of the two this adapter raises
    decides whether a charge survives. An account whose catalog has not been
    published yet refuses every event in every batch — as terminal that is the
    whole backlog abandoned on the first drain, with a durable error per row and
    no way back, where publishing the catalog would have made the identical
    request succeed. An event Stripe has aged out is the opposite: no number of
    attempts changes the answer, so it is stated as terminal here and not
    discovered by exhausting a retry schedule.
    """

    with _refusing_client() as client, pytest.raises(UpstreamUnavailableError):
        StripeBilling(client=client).record_meter_event(
            event_name="lazycloud_compute_cost_nanos",
            provider_customer_id="cus_meter",
            value_nanos=1_500,
            occurred_at=utc_now() - timedelta(minutes=1),
            identifier="usage-recent",
            pricing_version="2026-08-13.a",
        )

    with _refusing_on_no_request() as client, pytest.raises(InvalidInputError):
        StripeBilling(client=client).record_meter_event(
            event_name="lazycloud_compute_cost_nanos",
            provider_customer_id="cus_meter",
            value_nanos=1_500,
            occurred_at=utc_now() - timedelta(days=METER_EVENT_BACKFILL_DAYS + 1),
            identifier="usage-aged-out",
            pricing_version="2026-08-13.a",
        )
