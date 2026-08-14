from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs

import httpx
from provider_stripe import API_BASE_URL, CREDIT_GRANT_SETTLEMENT_GRACE, StripeBilling
from shared.timestamps import utc_now


def test_an_allowance_waits_only_for_the_cycle_before_it() -> None:
    """When a grant becomes spendable is a fact about its predecessor.

    Credit is applied when an invoice is *finalized* rather than when it is
    raised, and a subscription invoice finalizes about an hour after its period
    ends. So a cycle keeps claiming credit after it has ended, and a grant bought
    for the cycle that follows must not be reachable while it does — an invoice
    that overran its own allowance would take the difference out of the next
    one's, and overrun is the design here rather than an edge case.

    None of that is true of a cycle nothing precedes. Stripe refuses any
    `effective_at` at or before their own now, so an allowance that is spendable
    immediately is one they stamp themselves, on the clock the customer's
    subscription runs on. Sending nothing is what says that, and holding the
    first cycle back instead would be a customer told they have an allowance and
    charged for the three days before they could spend it.
    """

    sent: list[dict[str, list[str]]] = []
    # Whole seconds, because that is the resolution the provider is given one in.
    cycle_ended_at = (utc_now() + timedelta(days=30)).replace(microsecond=0)
    previous_cycle_ended_at = cycle_ended_at - timedelta(days=30)

    def handler(request: httpx.Request) -> httpx.Response:
        fields = parse_qs(request.content.decode())
        sent.append(fields)
        return httpx.Response(
            200,
            content=json.dumps(
                {
                    "id": f"credgr_test_{len(sent)}",
                    "amount": {"monetary": {"value": 10_000, "currency": "usd"}},
                    "expires_at": int(fields["expires_at"][0]),
                }
            ).encode(),
        )

    billing = StripeBilling(
        client=httpx.Client(base_url=API_BASE_URL, transport=httpx.MockTransport(handler))
    )
    for previous_period_ended_at in (None, previous_cycle_ended_at):
        billing.create_credit_grant(
            account_id="4a1d0f2c-6f5e-4a3b-8c9d-0e1f2a3b4c5d",
            provider_customer_id="cus_test",
            amount_nanos=100_000_000_000,
            period_ended_at=cycle_ended_at,
            previous_period_ended_at=previous_period_ended_at,
        )

    first, following = sent
    assert "effective_at" not in first
    assert datetime.fromtimestamp(int(following["effective_at"][0]), tz=UTC) == (
        previous_cycle_ended_at + CREDIT_GRANT_SETTLEMENT_GRACE
    )
    for fields in sent:
        assert datetime.fromtimestamp(int(fields["expires_at"][0]), tz=UTC) == (
            cycle_ended_at + CREDIT_GRANT_SETTLEMENT_GRACE
        )


def test_an_allowance_that_cannot_be_expired_yet_is_voided_instead() -> None:
    """Ending an allowance may not fail on a plan change already paid for.

    Stripe refuses to expire a grant that has not become effective yet, which is
    exactly what a plan change finds the outgoing allowance to be while the cycle
    before is still settling. The prorated difference has been charged and
    collected by then, so a refusal here is a customer billed for a plan this
    platform never recorded them on. Nothing can have been spent from a grant
    that was never spendable, so voiding it ends it at no cost.
    """

    called: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        called.append(request.url.path)
        if request.url.path.endswith("/expire"):
            return httpx.Response(
                400,
                content=json.dumps(
                    {
                        "error": {
                            "message": (
                                "Can't expire a credit grant that is not yet effective. Credit "
                                "grant credgr_test_1 will become effective on 1786982102"
                            )
                        }
                    }
                ).encode(),
            )
        # A grant bought for a cycle that has opened and not yet closed: live,
        # never voided, and days away from the expiry it was given.
        grant: dict[str, object] = {
            "id": "credgr_test_1",
            "amount": {"monetary": {"value": 10_000, "currency": "usd"}},
            "expires_at": int((utc_now() + timedelta(days=30)).timestamp()),
        }
        if request.url.path.endswith("/void"):
            grant["voided_at"] = int(utc_now().timestamp())
        return httpx.Response(200, content=json.dumps(grant).encode())

    billing = StripeBilling(
        client=httpx.Client(base_url=API_BASE_URL, transport=httpx.MockTransport(handler))
    )
    billing.expire_credit_grant(provider_credit_grant_id="credgr_test_1")

    assert called == [
        "/v1/billing/credit_grants/credgr_test_1",
        "/v1/billing/credit_grants/credgr_test_1/expire",
        "/v1/billing/credit_grants/credgr_test_1/void",
    ]
