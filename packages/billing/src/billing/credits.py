from __future__ import annotations

from datetime import datetime, timedelta
from fractions import Fraction
from itertools import groupby

from database.repositories.billing import BillingAccountRepository
from database.repositories.billing_allowance import BillingAllowanceRepository
from database.repositories.billing_costs import BillingLedgerCostRepository
from database.repositories.billing_credits import BillingCreditRepository
from database.repositories.billing_ledger import BillingLedgerRepository
from database.repositories.billing_outbox import (
    BillingMeterOutboxRepository,
    UndeliveredMeterTotals,
)
from database.tables.billing_ledger import BillingLedgerSegmentTable
from database.tables.billing_outbox import BillingMeterOutboxTable
from database.tables.identity import WorkspaceMemberTable
from database.tables.observability import UsageRecordTable
from shared.billing_accounts import BillingAccount
from shared.billing_credits import CreditGrant, CreditKind, CreditScope
from shared.billing_plans import BillingPlanId
from shared.billing_quotes import BILLED_METRICS
from shared.billing_rate_card import (
    ONE_TIME_TRIAL_NANOS,
    TRIAL_CREDIT_SCOPE,
    TRIAL_VALIDITY_DAYS,
    subscription_terms,
)
from shared.errors import ConflictError, UpstreamUnavailableError
from shared.payments import (
    METER_EVENT_NAMES,
    ProviderCreditApplicability,
    ProviderPaidSubscriptionPeriod,
    ProviderSubscription,
    SubscriptionPaymentProvider,
)
from shared.timestamps import to_utc, utc_now
from shared.usage import METERING_WINDOW_STARTED_AT_METADATA_KEY
from sqlalchemy import select
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
        funded=False,
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
    scope = (
        CreditScope.Compute
        if any(
            subscription_terms(line.terms_version).credit_scope is CreditScope.Compute
            for line in positive
        )
        else CreditScope.AllMetered
    )
    return CreditGrant(
        source_id=_subscription_source(
            min(positive, key=lambda line: line.provider_invoice_line_id)
        ),
        kind=CreditKind.Subscription,
        scope=scope,
        amount_nanos=amount,
        effective_at=to_utc(effective_at),
        expires_at=to_utc(period_ended_at),
    )


def initialize_local_credits(
    session: Session,
    payments: SubscriptionPaymentProvider,
    *,
    user_id: str,
    provider_customer_id: str,
    effective_at: datetime,
) -> None:
    credits = BillingCreditRepository(session)
    if credits.cutover(user_id=user_id) is not None:
        return
    grants = payments.credit_grants_for(provider_customer_id=provider_customer_id)
    if grants:
        raise ConflictError(
            "the payment customer has credit history; reconcile it before local provisioning"
        )
    credits.prepare_cutover(user_id=user_id, effective_at=effective_at)
    credits.complete_cutover(user_id=user_id, at=utc_now())
    credits.issue(
        user_id=user_id,
        grant=CreditGrant(
            source_id=f"trial:{user_id}",
            kind=CreditKind.Trial,
            scope=TRIAL_CREDIT_SCOPE,
            amount_nanos=ONE_TIME_TRIAL_NANOS,
            effective_at=to_utc(effective_at),
            expires_at=to_utc(effective_at) + timedelta(days=TRIAL_VALIDITY_DAYS),
        ),
    )


def reconcile_credit_cutover(
    session: Session,
    payments: SubscriptionPaymentProvider,
    *,
    account: BillingAccount,
    subscription: ProviderSubscription,
    at: datetime,
) -> str:
    credits = BillingCreditRepository(session)
    locked = BillingAccountRepository(session).get_by_user(account.user_id, for_update=True)
    if locked is None or (
        locked.provider_customer_id != account.provider_customer_id
        or locked.provider_subscription_id != subscription.provider_subscription_id
    ):
        raise ConflictError("billing identity changed during credit reconciliation")
    account = locked
    cutover = credits.cutover(user_id=account.user_id)
    if cutover is None:
        cutover = credits.prepare_cutover(
            user_id=account.user_id, effective_at=subscription.current_period_ended_at
        )
    if cutover.completed_at is not None:
        _recover_period_funding(
            session,
            payments,
            account=account,
            subscription=subscription,
            since=cutover.effective_at,
        )
        return ""
    if at < cutover.effective_at:
        return ""
    reason = _legacy_settlement_gap(
        session, payments, account=account, boundary=cutover.effective_at
    )
    if reason:
        credits.block_cutover(user_id=account.user_id, reason=reason)
        return reason
    grants = payments.credit_grants_for(provider_customer_id=account.provider_customer_id)
    for grant in grants:
        if grant.available_balance_nanos != grant.ledger_balance_nanos:
            reason = (
                f"credit grant {grant.provider_credit_grant_id} has unsettled invoice reservations"
            )
            credits.block_cutover(user_id=account.user_id, reason=reason)
            return reason
        if grant.ledger_balance_nanos < 0:
            reason = (
                f"credit grant {grant.provider_credit_grant_id} has a negative provider balance"
            )
            credits.block_cutover(user_id=account.user_id, reason=reason)
            return reason
        if grant.ledger_balance_nanos == 0:
            continue
        if grant.category == "paid":
            reason = (
                f"purchased grant {grant.provider_credit_grant_id} requires confirmed payment "
                "and consumption history before its remaining funds can be transferred"
            )
            credits.block_cutover(user_id=account.user_id, reason=reason)
            return reason
        if (
            grant.provider_credit_grant_id != account.provider_credit_grant_id
            or grant.category != "promotional"
            or grant.applicability is not ProviderCreditApplicability.AllMetered
        ):
            reason = (
                f"credit grant {grant.provider_credit_grant_id} "
                "is not the recorded subscription allowance"
            )
            credits.block_cutover(user_id=account.user_id, reason=reason)
            return reason
    for grant in grants:
        if grant.ledger_balance_nanos > 0:
            payments.expire_credit_grant(provider_credit_grant_id=grant.provider_credit_grant_id)
    remaining = payments.credit_grants_for(provider_customer_id=account.provider_customer_id)
    if any(
        grant.available_balance_nanos != 0 or grant.ledger_balance_nanos != 0 for grant in remaining
    ):
        reason = "legacy provider credit balances have not settled to zero"
        credits.block_cutover(user_id=account.user_id, reason=reason)
        return reason
    credits.complete_cutover(user_id=account.user_id, at=at)
    _recover_period_funding(
        session,
        payments,
        account=account,
        subscription=subscription,
        since=cutover.effective_at,
    )
    BillingLedgerRepository(session).settle_pending_credits(owner_user_id=account.user_id)
    return ""


def _legacy_settlement_gap(
    session: Session,
    payments: SubscriptionPaymentProvider,
    *,
    account: BillingAccount,
    boundary: datetime,
) -> str:
    unpriced = session.scalars(
        select(UsageRecordTable)
        .join(
            WorkspaceMemberTable, WorkspaceMemberTable.workspace_id == UsageRecordTable.workspace_id
        )
        .where(
            WorkspaceMemberTable.user_id == account.user_id,
            WorkspaceMemberTable.role == "owner",
            UsageRecordTable.metric.in_([metric.value for metric in BILLED_METRICS]),
            ~select(BillingLedgerSegmentTable.id)
            .where(BillingLedgerSegmentTable.usage_record_id == UsageRecordTable.id)
            .exists(),
        )
    ).all()
    for record in unpriced:
        metadata = record.payload.get("metadata")
        start = (
            metadata.get(METERING_WINDOW_STARTED_AT_METADATA_KEY)
            if isinstance(metadata, dict)
            else None
        )
        if not isinstance(start, str):
            return f"usage {record.id} has no priced metering window"
        try:
            started_at = datetime.fromisoformat(start)
        except ValueError:
            return f"usage {record.id} has an invalid metering window"
        if started_at.tzinfo is None or to_utc(started_at) < boundary:
            return f"legacy usage {record.id} remains unpriced"
    pending = session.scalar(
        select(BillingMeterOutboxTable)
        .where(
            BillingMeterOutboxTable.provider_customer_id == account.provider_customer_id,
            BillingMeterOutboxTable.occurred_at < boundary,
            BillingMeterOutboxTable.status.not_in(("sent", "waived")),
        )
        .limit(1)
    )
    if pending is not None:
        return f"legacy usage {pending.identifier} remains {pending.status}"
    invoices = payments.invoices_for(
        provider_customer_id=account.provider_customer_id,
        since=account.created_at,
        limit=None,
    )
    legacy = [
        invoice
        for invoice in invoices
        if invoice.period_ended_at <= boundary
        and invoice.period_started_at < invoice.period_ended_at
    ]
    if not any(invoice.period_ended_at == boundary for invoice in legacy):
        return "the prior subscription period has no finalized invoice at the cutover boundary"
    for invoice in legacy:
        if invoice.status != "paid":
            return f"legacy invoice {invoice.provider_invoice_id} remains {invoice.status}"
        priced = BillingLedgerCostRepository(session).account_dimension_totals(
            user_id=account.user_id,
            start=invoice.period_started_at,
            end=invoice.period_ended_at,
        )
        undelivered = BillingMeterOutboxRepository(session).undelivered_totals(
            provider_customer_id=account.provider_customer_id,
            started_at=invoice.period_started_at,
            ended_at=invoice.period_ended_at,
        )
        invoiced = payments.invoice_metered_totals(provider_invoice_id=invoice.provider_invoice_id)
        if any(
            priced.get(dimension, 0)
            - undelivered.get(event_name, UndeliveredMeterTotals()).waived_nanos
            != invoiced.get(event_name, 0)
            for dimension, event_name in METER_EVENT_NAMES.items()
        ):
            return (
                f"legacy invoice {invoice.provider_invoice_id} disagrees with exported ledger usage"
            )
    return ""


def _recover_period_funding(
    session: Session,
    payments: SubscriptionPaymentProvider,
    *,
    account: BillingAccount,
    subscription: ProviderSubscription,
    since: datetime,
) -> None:
    periods = BillingAllowanceRepository(session).unconfirmed_periods(
        user_id=account.user_id,
        since=since,
        before=subscription.current_period_started_at,
    )
    if not periods:
        return
    evidence = payments.paid_subscription_periods(
        provider_customer_id=account.provider_customer_id,
        provider_subscription_id=subscription.provider_subscription_id,
        since=since,
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


__all__ = ["fund_subscription_credits", "initialize_local_credits", "reconcile_credit_cutover"]
