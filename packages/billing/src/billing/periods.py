from __future__ import annotations

from database.repositories.billing_allowance import (
    BillingAllowanceRepository,
    SubscriptionPeriodOutcome,
)
from shared.billing_plans import BillingPlanId
from shared.billing_rate_card import published_plan
from shared.payments import PaymentProvider, ProviderSubscription
from sqlalchemy.orm import Session


def carry_plan_into_cycle(
    session: Session,
    payments: PaymentProvider,
    *,
    account_id: str,
    provider_customer_id: str,
    provider_credit_grant_id: str,
    subscription: ProviderSubscription,
    plan: BillingPlanId,
) -> str:
    """Give this cycle the terms the plan comes with, and buy them once.

    Takes the grant the account currently names and returns the grant that funds
    the cycle once this has run — the same one where nothing was bought, so a
    caller writes what comes back without having to work out whether it changed.

    The period is written first and what happens to the grants is decided from
    what that did to it. A renewal and a plan change both reach here by more than
    one route — a renewal by two deliveries describing one event, a plan change
    by a retry after a transaction that died — and a grant is money given away,
    so the period is what settles which caller buys the allowance and which finds
    it already bought.

    Opening a cycle leaves the outgoing grant alone: that grant is what funds the
    invoice finalizing at that moment, and expiring it would take back an
    allowance the customer has already been invoiced against. Re-terming the
    cycle in progress is the opposite — one plan swapped for another inside a
    single cycle — and its outgoing grant is expired before the replacement is
    bought, because two live grants are two allowances for one cycle and nothing
    downstream can tell which of them a charge was spent on.

    Expiring before buying is deliberate: a failure between the two leaves the
    cycle unfunded until the next attempt or the next renewal, where the other
    order would leave the customer holding both and nothing to notice it.

    When the allowance becomes spendable is decided from the cycle before it,
    which the same write reports. A cycle that follows one keeps its allowance
    out of reach until the invoice that cycle raises has been settled; an
    account's first cycle follows nothing, so holding its allowance back would
    only be a customer denied for three days what they were told they had. The
    two are told apart by the rows rather than by which caller is asking, for the
    reason the grant itself is: registration and a plan change and a renewal all
    reach here, and only the period knows which cycle it is funding.

    Every caller holds the account row lock before reaching here, which is what
    makes the read-then-write inside safe against a delivery arriving mid-change.
    """

    included_nanos = published_plan(plan).included_nanos
    written = BillingAllowanceRepository(session).set_subscription_period(
        user_id=account_id,
        period_started_at=subscription.current_period_started_at,
        period_ended_at=subscription.current_period_ended_at,
        allowance_nanos=included_nanos,
    )
    if written.outcome is SubscriptionPeriodOutcome.Unchanged:
        return provider_credit_grant_id
    if written.outcome is SubscriptionPeriodOutcome.ReTermed and provider_credit_grant_id:
        payments.expire_credit_grant(provider_credit_grant_id=provider_credit_grant_id)
    return payments.create_credit_grant(
        account_id=account_id,
        provider_customer_id=provider_customer_id,
        amount_nanos=included_nanos,
        period_ended_at=subscription.current_period_ended_at,
        previous_period_ended_at=written.previous_period_ended_at,
    ).provider_credit_grant_id


__all__ = ["carry_plan_into_cycle"]
