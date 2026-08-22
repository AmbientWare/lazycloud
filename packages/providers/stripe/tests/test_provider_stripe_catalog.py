from __future__ import annotations

import json
from urllib.parse import parse_qs

import httpx
from provider_stripe.catalog import (
    METERED_PRICE_LOOKUP_KEYS,
    NANODOLLAR_UNIT_AMOUNT,
    PLAN_LINES,
    USAGE_LINES,
    StripeCatalog,
)
from shared.billing_plans import BillingPlanId

_TEAM = next(line for line in PLAN_LINES if line.plan is BillingPlanId.Team)
_RETIRED_PRICE_ID = "price_the_old_one"


def test_a_changed_plan_price_moves_its_lookup_key_and_retires_the_old_price() -> None:
    """The rate card decides what a plan costs, and a price cannot be edited.

    So the figure moves by publishing a new price carrying the same lookup key
    and retiring the one it replaced. Anything resolving a plan by lookup key
    then finds the current figure, which is what a subscription opened after this
    is built from.

    The retirement matters as much as the transfer. A price left active with no
    lookup key is one nothing resolves to and nobody notices, until it turns up
    on an invoice somebody is still subscribed against.
    """

    sent: list[tuple[str, str, dict[str, list[str]]]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        fields = parse_qs(request.content.decode()) if request.content else {}
        sent.append((request.method, path, fields))

        if path.endswith("/account"):
            return httpx.Response(200, content=json.dumps({"id": "acct_test"}))
        if path.endswith("/billing/meters"):
            return httpx.Response(
                200,
                content=json.dumps(
                    {
                        "data": [
                            {
                                "id": f"mtr_{line.meter_event_name}",
                                "event_name": line.meter_event_name,
                            }
                            for line in USAGE_LINES
                        ]
                    }
                ),
            )
        if path.endswith("/products"):
            return httpx.Response(
                200,
                content=json.dumps(
                    {"data": [{"id": line.product_id} for line in (*PLAN_LINES, *USAGE_LINES)]}
                ),
            )
        if request.method == "GET" and path.endswith("/prices"):
            # Everything published and agreeing, except the Team plan, which the
            # account holds at a figure the rate card has moved off.
            data: list[dict[str, object]] = [
                {
                    "id": f"price_{line.price_lookup_key}",
                    "lookup_key": line.price_lookup_key,
                    "unit_amount_decimal": NANODOLLAR_UNIT_AMOUNT,
                    "recurring": {"meter": f"mtr_{line.meter_event_name}"},
                }
                for line in USAGE_LINES
            ]
            data.extend(
                {
                    "id": f"price_{line.price_lookup_key}",
                    "lookup_key": line.price_lookup_key,
                    "unit_amount": 0,
                }
                for line in PLAN_LINES
                if line.plan is BillingPlanId.Free
            )
            data.append(
                {
                    "id": _RETIRED_PRICE_ID,
                    "lookup_key": _TEAM.price_lookup_key,
                    "unit_amount": 15_000,
                }
            )
            return httpx.Response(200, content=json.dumps({"data": data}))
        return httpx.Response(200, content=json.dumps({"id": "price_the_new_one"}))

    client = httpx.Client(transport=httpx.MockTransport(handler), base_url="https://stripe.invalid")
    try:
        StripeCatalog(client=client).publish(
            plan_prices={BillingPlanId.Free: 0, BillingPlanId.Team: 200_000_000_000}
        )
    finally:
        client.close()

    created = [
        fields
        for method, path, fields in sent
        if method == "POST" and path.endswith("/prices") and "transfer_lookup_key" in fields
    ]
    assert len(created) == 1
    assert created[0]["lookup_key"] == [_TEAM.price_lookup_key]
    assert created[0]["transfer_lookup_key"] == ["true"]
    assert created[0]["unit_amount"] == ["20000"]

    retired = [
        fields
        for method, path, fields in sent
        if method == "POST" and path.endswith(f"/prices/{_RETIRED_PRICE_ID}")
    ]
    assert retired == [{"active": ["false"]}]

    # Nothing that agrees is rewritten, and no metered price is touched: their
    # amount is a fixed conversion and the rate card is applied before the usage
    # is reported.
    rewritten = [
        fields.get("lookup_key", [""])[0]
        for method, path, fields in sent
        if method == "POST" and path.endswith("/prices")
    ]
    assert set(rewritten).isdisjoint(METERED_PRICE_LOOKUP_KEYS)
