from __future__ import annotations

from datetime import datetime, timedelta
from fractions import Fraction
from itertools import groupby

from database.repositories.billing import BillingAccountRepository
from database.repositories.billing_allowance import BillingAllowanceRepository
from database.repositories.billing_credits import BillingCreditRepository
from database.repositories.billing_ledger import BillingLedgerRepository
from shared.billing_accounts import BillingAccount
from shared.billing_credits import CreditGrant, CreditKind
from shared.billing_plans import BillingPlanId
from shared.billing_rate_card import (
    ONE_TIME_TRIAL_NANOS,
    TRIAL_VALIDITY_DAYS,
    subscription_terms,
)
from shared.errors import ConflictError, UpstreamUnavailableError
from shared.payments import (
    ProviderPaidSubscriptionPeriod,
    ProviderSubscription,
    SubscriptionPaymentProvider,
)
from shared.timestamps import to_utc, utc_now
from sqlalchemy.orm import Session


def fund_subscription_credits(
    session: Session,
    payments: SubscriptionPaymentProvider,
    *,
    user_id: str,
    provider_customer_id: str,
    subscription: ProviderSubscription,
) -> bool:
    BillingAccountRepository(session).get_by_user(user_id, for_update=True)
    if subscription.terms_version is None:
        raise UpstreamUnavailableError("subscription terms await a recognized provider price")
    target = subscription_terms(subscription.terms_version)
    credits = BillingCreditRepository(session)
    allowances = BillingAllowanceRepository(session)
    period = allowances.current_period(user_id=user_id, at=subscription.current_period_started_at)
    if period is None or period.started_at != subscription.current_period_started_at:
        raise ConflictError("subscription funding requires its recorded period")
    highest = (
        subscription_terms(period.funded_terms_version)
        if period.funded_terms_version is not None
        else None
    )
    sources = credits.subscription_sources(
        user_id=user_id, period_ended_at=subscription.current_period_ended_at
    )
    evidence = (
        payments.paid_subscription_periods(
            provider_customer_id=provider_customer_id,
            provider_subscription_id=subscription.provider_subscription_id,
            since=subscription.current_period_started_at,
        )
        if target.monthly_nanos > 0 or period.allowance_nanos > 0
        else ()
    )
    eligible: list[ProviderPaidSubscriptionPeriod] = []
    for funded in evidence:
        if (
            funded.invoice_paid_nanos <= 0
            or funded.amount_nanos == 0
            or funded.period_ended_at != subscription.current_period_ended_at
            or funded.period_started_at < subscription.current_period_started_at
        ):
            continue
        terms = subscription_terms(funded.terms_version)
        if (
            terms.plan is not funded.plan
            or funded.provider_subscription_id != subscription.provider_subscription_id
            or funded.period_started_at >= funded.period_ended_at
        ):
            raise UpstreamUnavailableError("paid invoice plan disagrees with its frozen terms")
        eligible.append(funded)
        if funded.amount_nanos > 0 and (
            highest is None or terms.monthly_nanos > highest.monthly_nanos
        ):
            highest = terms
    for _, grouped in groupby(
        sorted(eligible, key=lambda line: line.provider_invoice_id),
        key=lambda line: line.provider_invoice_id,
    ):
        lines = tuple(grouped)
        if any(_subscription_source(line) in sources for line in lines):
            continue
        grant = _invoice_subscription_grant(
            lines, period_started_at=period.started_at, period_ended_at=period.ended_at
        )
        if grant is not None:
            credits.issue(user_id=user_id, grant=grant)
    allowances.set_subscription_period(
        user_id=user_id,
        period_started_at=period.started_at,
        period_ended_at=period.ended_at,
        allowance_nanos=credits.subscription_issued(
            user_id=user_id, period_ended_at=period.ended_at
        ),
    )
    if highest is None and target.monthly_nanos == 0 and period.allowance_nanos == 0:
        highest = target
    if highest is not None:
        allowances.record_funded_terms(
            user_id=user_id, period_started_at=period.started_at, terms_version=highest.version
        )
    if target.monthly_nanos > 0 and (
        highest is None or highest.monthly_nanos < target.monthly_nanos
    ):
        return False
    allowances.confirm_credit(user_id=user_id, period_started_at=period.started_at, at=utc_now())
    BillingLedgerRepository(session).settle_pending_credits(owner_user_id=user_id)
    return True


def _subscription_source(line: ProviderPaidSubscriptionPeriod) -> str:
    return f"subscription:{line.provider_invoice_id}:{line.provider_invoice_line_id}"


def _invoice_subscription_grant(
    lines: tuple[ProviderPaidSubscriptionPeriod, ...],
    *,
    period_started_at: datetime,
    period_ended_at: datetime,
) -> CreditGrant | None:
    positive = tuple(line for line in lines if line.amount_nanos > 0)
    if not positive:
        return None
    if len({(line.invoice_paid_nanos, line.paid_at) for line in lines}) != 1:
        raise UpstreamUnavailableError("paid invoice lines disagree about their payment")
    duration = (period_ended_at - period_started_at) // timedelta(microseconds=1)
    included = Fraction(0)
    for line in lines:
        terms = subscription_terms(line.terms_version)
        if terms.monthly_nanos == 0:
            continue
        covered = (line.period_ended_at - line.period_started_at) // timedelta(microseconds=1)
        fraction = Fraction(covered, duration)
        if line.amount_nanos > 0:
            if line.prorated:
                fraction = min(fraction, Fraction(line.amount_nanos, terms.monthly_nanos))
            included += terms.included_nanos * fraction
        else:
            included -= terms.included_nanos * fraction
    amount = max(0, included.numerator // included.denominator)
    effective_at = max(
        max(line.paid_at, line.period_started_at) if line.prorated else line.period_started_at
        for line in positive
    )
    if amount == 0 or effective_at >= period_ended_at:
        return None
    return CreditGrant(
        source_id=_subscription_source(
            min(positive, key=lambda line: line.provider_invoice_line_id)
        ),
        kind=CreditKind.Subscription,
        amount_nanos=amount,
        effective_at=to_utc(effective_at),
        expires_at=to_utc(period_ended_at),
    )


def initialize_local_credits(
    session: Session,
    *,
    user_id: str,
    effective_at: datetime,
) -> None:
    credits = BillingCreditRepository(session)
    BillingAccountRepository(session).get_by_user(user_id, for_update=True)
    source_id = f"trial:{user_id}"
    if credits.has_grant(user_id=user_id, source_id=source_id):
        return
    credits.issue(
        user_id=user_id,
        grant=CreditGrant(
            source_id=source_id,
            kind=CreditKind.Trial,
            amount_nanos=ONE_TIME_TRIAL_NANOS,
            effective_at=to_utc(effective_at),
            expires_at=to_utc(effective_at) + timedelta(days=TRIAL_VALIDITY_DAYS),
        ),
    )


def recover_subscription_credits(
    session: Session,
    payments: SubscriptionPaymentProvider,
    *,
    account: BillingAccount,
    subscription: ProviderSubscription,
) -> None:
    locked = BillingAccountRepository(session).get_by_user(account.user_id, for_update=True)
    if locked is None or (
        locked.provider_customer_id != account.provider_customer_id
        or locked.provider_subscription_id != subscription.provider_subscription_id
    ):
        raise ConflictError("billing identity changed during credit reconciliation")
    since = account.created_at
    if since >= subscription.current_period_started_at:
        return
    evidence = payments.paid_subscription_periods(
        provider_customer_id=account.provider_customer_id,
        provider_subscription_id=subscription.provider_subscription_id,
        since=since,
    )
    allowances = BillingAllowanceRepository(session)
    for line in evidence:
        if (
            line.prorated
            or line.invoice_paid_nanos <= 0
            or line.amount_nanos <= 0
            or line.period_started_at < since
            or line.period_ended_at > subscription.current_period_started_at
        ):
            continue
        if (
            line.provider_subscription_id != subscription.provider_subscription_id
            or subscription_terms(line.terms_version).plan is not line.plan
            or line.period_started_at >= line.period_ended_at
        ):
            raise UpstreamUnavailableError(
                "paid renewal does not establish valid subscription terms"
            )
        period = allowances.current_period(user_id=account.user_id, at=line.period_started_at)
        if period is not None:
            if (period.started_at, period.ended_at) != (
                line.period_started_at,
                line.period_ended_at,
            ):
                raise ConflictError("paid renewal disagrees with its recorded subscription period")
            continue
        # A failed renewal may never have reached the local period owner.
        # Proration lines cannot establish the missing cycle's full interval.
        allowances.set_subscription_period(
            user_id=account.user_id,
            period_started_at=line.period_started_at,
            period_ended_at=line.period_ended_at,
            allowance_nanos=0,
        )
    periods = allowances.unconfirmed_periods(
        user_id=account.user_id,
        since=since,
        before=subscription.current_period_started_at,
    )
    for period in periods:
        funded = max(
            (
                line
                for line in evidence
                if line.period_started_at >= period.started_at
                and line.period_ended_at == period.ended_at
                and (
                    line.plan is BillingPlanId.Free
                    or (line.invoice_paid_nanos > 0 and line.amount_nanos > 0)
                )
            ),
            key=lambda line: subscription_terms(line.terms_version).monthly_nanos,
            default=None,
        )
        if funded is None:
            continue
        fund_subscription_credits(
            session,
            payments,
            user_id=account.user_id,
            provider_customer_id=account.provider_customer_id,
            subscription=ProviderSubscription(
                provider_subscription_id=subscription.provider_subscription_id,
                status=subscription.status,
                current_period_started_at=period.started_at,
                current_period_ended_at=period.ended_at,
                plan=funded.plan,
                terms_version=funded.terms_version,
                scheduled_terms_version=None,
                scheduled_change_at=None,
            ),
        )


__all__ = ["fund_subscription_credits", "initialize_local_credits", "recover_subscription_credits"]
