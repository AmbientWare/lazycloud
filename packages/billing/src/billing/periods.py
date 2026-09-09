from __future__ import annotations

from database.repositories.billing_allowance import (
    BillingAllowanceRepository,
    SubscriptionPeriodOutcome,
)
from database.repositories.billing_credits import BillingCreditRepository
from shared.billing_plans import BillingPlanId
from shared.billing_rate_card import published_plan
from shared.errors import UpstreamUnavailableError
from shared.payments import ProviderSubscription, SubscriptionPaymentProvider
from sqlalchemy.orm import Session

from billing.credits import fund_subscription_credits


def carry_plan_into_cycle(
    session: Session,
    payments: SubscriptionPaymentProvider,
    *,
    account_id: str,
    provider_customer_id: str,
    provider_credit_grant_id: str,
    subscription: ProviderSubscription,
    plan: BillingPlanId,
) -> str:
    """Record this cycle's terms and fund the included credit once.

    Callers hold the billing account lock. Local credits require payment evidence
    for paid plans; legacy periods retain their provider grant until cutover.
    """

    written = BillingAllowanceRepository(session).set_subscription_period(
        user_id=account_id,
        period_started_at=subscription.current_period_started_at,
        period_ended_at=subscription.current_period_ended_at,
        allowance_nanos=published_plan(plan).included_nanos,
        funded=True,
    )
    cutover = BillingCreditRepository(session).cutover(user_id=account_id)
    if cutover is not None and subscription.current_period_started_at >= cutover.effective_at:
        confirmed = fund_subscription_credits(
            session,
            payments,
            user_id=account_id,
            provider_customer_id=provider_customer_id,
            subscription=subscription,
            plan=plan,
            allowance_nanos=written.allowance_nanos,
        )
        if not confirmed:
            raise UpstreamUnavailableError(
                "paid subscription credits await a matching paid invoice line"
            )
        return provider_credit_grant_id
    if plan is BillingPlanId.Free or written.outcome is SubscriptionPeriodOutcome.Unchanged:
        return provider_credit_grant_id
    if written.outcome is SubscriptionPeriodOutcome.ReTermed and provider_credit_grant_id:
        payments.expire_credit_grant(provider_credit_grant_id=provider_credit_grant_id)
    return payments.create_credit_grant(
        account_id=account_id,
        provider_customer_id=provider_customer_id,
        amount_nanos=written.allowance_nanos,
        period_ended_at=subscription.current_period_ended_at,
        previous_period_ended_at=written.previous_period_ended_at,
    ).provider_credit_grant_id


__all__ = ["carry_plan_into_cycle"]
