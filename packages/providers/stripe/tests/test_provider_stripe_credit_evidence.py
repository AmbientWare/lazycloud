from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest
from provider_stripe import API_BASE_URL, StripeBilling
from shared.billing_plans import BillingPlanId, SubscriptionTermsVersion


@pytest.mark.parametrize(
    ("lookup_key", "amount_cents", "version"),
    [
        ("lazycloud_plan_team_monthly_usd", 10000, SubscriptionTermsVersion.TeamLegacy),
        ("lazycloud_plan_team_v2_monthly_usd", 4900, SubscriptionTermsVersion.TeamV2),
        ("lazycloud_plan_team_v3_monthly_usd", 4900, SubscriptionTermsVersion.Team),
    ],
)
def test_paid_plan_proof_uses_paginated_subscription_lines_including_paid_proration(
    lookup_key: str, amount_cents: int, version: SubscriptionTermsVersion
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/subscriptions/sub_current"):
            return httpx.Response(
                200,
                json={
                    "id": "sub_current",
                    "customer": "cus_owner",
                    "status": "active",
                    "items": {
                        "data": [
                            {
                                "id": "si_plan",
                                "price": {
                                    "id": "price_plan_old",
                                    "product": "lazycloud_plan_team",
                                    "lookup_key": lookup_key,
                                    "currency": "usd",
                                    "unit_amount": amount_cents,
                                    "recurring": {
                                        "interval": "month",
                                        "interval_count": 1,
                                        "usage_type": "licensed",
                                    },
                                },
                                "current_period_start": 1788220800,
                                "current_period_end": 1790812800,
                            }
                        ]
                    },
                },
            )
        if request.url.path.endswith("/invoices"):
            paid = bool(request.url.params.get("starting_after"))
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "id": "in_paid" if paid else "in_unpaid",
                            "customer": "cus_owner",
                            "status": "paid" if paid else "open",
                            "currency": "usd",
                            "amount_paid": 2900 if paid else 0,
                            "status_transitions": {"paid_at": 1788307200 if paid else None},
                            "period_start": 1788220800,
                            "period_end": 1790812800,
                        }
                    ],
                    "has_more": not paid,
                },
            )
        if request.url.path.endswith("/in_paid/lines"):
            second = bool(request.url.params.get("starting_after"))
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "id": "il_upgrade" if second else "il_other_subscription",
                            "amount": 2900,
                            "currency": "usd",
                            "period": {"start": 1788307200, "end": 1790812800},
                            "pricing": {
                                "price_details": {
                                    "price": {
                                        "id": "price_plan_old",
                                        "product": "lazycloud_plan_team",
                                        "lookup_key": lookup_key,
                                        "currency": "usd",
                                        "unit_amount": amount_cents,
                                        "recurring": {
                                            "interval": "month",
                                            "interval_count": 1,
                                            "usage_type": "licensed",
                                        },
                                    }
                                }
                            },
                            "parent": {
                                "type": "subscription_item_details",
                                "subscription_item_details": {
                                    "subscription": "sub_current" if second else "sub_other",
                                    "proration": True,
                                },
                            },
                        }
                    ],
                    "has_more": not second,
                },
            )
        raise AssertionError("unexpected evidence resource")

    with httpx.Client(base_url=API_BASE_URL, transport=httpx.MockTransport(handler)) as client:
        billing = StripeBilling(client)
        since = datetime(2026, 9, 1, tzinfo=UTC)
        invoices = billing.invoices_for(provider_customer_id="cus_owner", since=since, limit=None)
        periods = billing.paid_subscription_periods(
            provider_customer_id="cus_owner",
            provider_subscription_id="sub_current",
            since=since,
        )
    assert [invoice.status for invoice in invoices] == ["open", "paid"]
    assert len(periods) == 1
    assert periods[0].provider_invoice_line_id == "il_upgrade"
    assert periods[0].plan is BillingPlanId.Team
    assert periods[0].terms_version is version
    assert periods[0].prorated
    assert periods[0].amount_nanos == periods[0].invoice_paid_nanos == 29_000_000_000
    assert periods[0].period_started_at == datetime(2026, 9, 2, tzinfo=UTC)
