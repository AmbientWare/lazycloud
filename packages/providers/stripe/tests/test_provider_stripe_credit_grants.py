from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs

import httpx
from provider_stripe import API_BASE_URL, CREDIT_GRANT_SETTLEMENT_GRACE, StripeBilling
from shared.timestamps import utc_now


def test_a_grant_is_spendable_on_its_own_invoice_and_on_no_other() -> None:
    """The window a grant applies in is its period, shifted past finalization.

    Stripe applies credit at finalization rather than when the invoice is raised,
    and a subscription invoice finalizes about an hour after the period ends. So
    the window moves at both ends, and each end answers a different way of paying
    the wrong month's usage out of this month's allowance.

    Expiring on the boundary means the grant is already gone when the invoice it
    was bought for asks for it, and the next period's allowance settles the last
    period's arrears. Observed against a real account before this: the credit
    transaction for cycle one named the grant issued for cycle two.

    Becoming effective on the boundary means the opposite — this grant is live
    when the *previous* period's invoice finalizes, so an invoice that overran
    its own allowance takes the difference out of this one. Overrun is the design
    rather than an edge case here, which makes that the ordinary path.

    The period is anchored on now so the shifted start is a future instant: a
    grant is never effective before it is bought, and the clamp that holds would
    otherwise be what this measured.
    """

    sent: dict[str, list[str]] = {}
    # Whole seconds, because that is the resolution the provider is given one in.
    period_started_at = (utc_now() + timedelta(minutes=1)).replace(microsecond=0)
    period_ended_at = period_started_at + timedelta(days=30)

    def handler(request: httpx.Request) -> httpx.Response:
        sent.update(parse_qs(request.content.decode()))
        return httpx.Response(
            200,
            content=json.dumps(
                {
                    "id": "credgr_test_1",
                    "amount": {"monetary": {"value": 10_000, "currency": "usd"}},
                    "expires_at": int(sent["expires_at"][0]),
                }
            ).encode(),
        )

    client = httpx.Client(base_url=API_BASE_URL, transport=httpx.MockTransport(handler))
    StripeBilling(client=client).create_credit_grant(
        account_id="4a1d0f2c-6f5e-4a3b-8c9d-0e1f2a3b4c5d",
        provider_customer_id="cus_test",
        amount_nanos=100_000_000_000,
        period_started_at=period_started_at,
        period_ended_at=period_ended_at,
    )

    effective_at = datetime.fromtimestamp(int(sent["effective_at"][0]), tz=UTC)
    expires_at = datetime.fromtimestamp(int(sent["expires_at"][0]), tz=UTC)
    assert effective_at > period_started_at
    assert expires_at > period_ended_at
    assert effective_at == period_started_at + CREDIT_GRANT_SETTLEMENT_GRACE
    assert expires_at == period_ended_at + CREDIT_GRANT_SETTLEMENT_GRACE
