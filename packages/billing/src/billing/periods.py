from __future__ import annotations

from database.repositories.billing_allowance import BillingAllowanceRepository
from database.repositories.billing_credits import BillingCreditRepository
from shared.billing_plans import BillingPlanId
from shared.billing_rate_card import subscription_terms
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
    subscription: ProviderSubscription,
    plan: BillingPlanId,
) -> None:
    """Record this cycle and fund local credit from confirmed payment evidence."""
    if subscription.terms_version is None:
        raise UpstreamUnavailableError("subscription terms await a recognized provider price")
    if subscription_terms(subscription.terms_version).plan is not plan:
        raise UpstreamUnavailableError("subscription plan disagrees with its price terms")
    credits = BillingCreditRepository(session)
    BillingAllowanceRepository(session).set_subscription_period(
        user_id=account_id,
        period_started_at=subscription.current_period_started_at,
        period_ended_at=subscription.current_period_ended_at,
        allowance_nanos=credits.subscription_issued(
            user_id=account_id, period_ended_at=subscription.current_period_ended_at
        ),
    )
    if not fund_subscription_credits(
        session,
        payments,
        user_id=account_id,
        provider_customer_id=provider_customer_id,
        subscription=subscription,
    ):
        raise UpstreamUnavailableError(
            "paid subscription credits await a matching paid invoice line"
        )


__all__ = ["carry_plan_into_cycle"]
