from __future__ import annotations

from database.repositories.billing_allowance import (
    BillingAllowanceRepository,
    SubscriptionPeriodOutcome,
)
from database.repositories.billing_credits import BillingCreditRepository
from shared.billing_plans import BillingPlanId, SubscriptionTermsVersion
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
    provider_credit_grant_id: str,
    subscription: ProviderSubscription,
    plan: BillingPlanId,
) -> str:
    """Record this cycle's terms and fund the included credit once.

    Callers hold the billing account lock. Local credits require payment evidence
    for paid plans; legacy periods retain their provider grant until cutover.
    """

    if subscription.terms_version is None:
        raise UpstreamUnavailableError("subscription terms await a recognized provider price")
    held_terms = subscription_terms(subscription.terms_version)
    if held_terms.plan is not plan:
        raise UpstreamUnavailableError("subscription plan disagrees with its price terms")
    credits = BillingCreditRepository(session)
    cutover = credits.cutover(user_id=account_id)
    local = cutover is not None and subscription.current_period_started_at >= cutover.effective_at
    if (
        not local
        and held_terms.monthly_nanos > 0
        and subscription.terms_version is not SubscriptionTermsVersion.TeamLegacy
    ):
        raise UpstreamUnavailableError(
            "prepaid subscription terms require the local credit transition"
        )
    written = BillingAllowanceRepository(session).set_subscription_period(
        user_id=account_id,
        period_started_at=subscription.current_period_started_at,
        period_ended_at=subscription.current_period_ended_at,
        allowance_nanos=(
            credits.subscription_issued(
                user_id=account_id, period_ended_at=subscription.current_period_ended_at
            )
            if local
            else (0 if plan is BillingPlanId.Free else held_terms.included_nanos)
        ),
        funded=not local,
    )
    if local:
        confirmed = fund_subscription_credits(
            session,
            payments,
            user_id=account_id,
            provider_customer_id=provider_customer_id,
            subscription=subscription,
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
