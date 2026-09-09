from __future__ import annotations

from datetime import datetime

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
from shared.errors import ConflictError
from shared.payments import (
    METER_EVENT_NAMES,
    ProviderCreditApplicability,
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
    plan: BillingPlanId,
    allowance_nanos: int,
) -> bool:
    credits = BillingCreditRepository(session)
    issued = credits.subscription_issued(
        user_id=user_id, period_ended_at=subscription.current_period_ended_at
    )
    now = utc_now()
    funding_id = "free"
    effective_at = subscription.current_period_started_at if issued == 0 else now
    if plan is not BillingPlanId.Free and issued < allowance_nanos:
        evidence = payments.paid_subscription_periods(
            provider_customer_id=provider_customer_id,
            provider_subscription_id=subscription.provider_subscription_id,
            since=subscription.current_period_started_at,
        )
        eligible = [
            period
            for period in evidence
            if period.plan is plan
            and period.invoice_paid_nanos > 0
            and period.amount_nanos > 0
            and period.period_ended_at == subscription.current_period_ended_at
            and period.period_started_at >= subscription.current_period_started_at
        ]
        if not eligible:
            return False
        funded = max(eligible, key=lambda period: period.paid_at)
        funding_id = funded.provider_invoice_id
        if issued:
            effective_at = funded.paid_at
    if allowance_nanos > issued:
        credits.issue(
            user_id=user_id,
            grant=CreditGrant(
                source_id=(
                    f"subscription:{funding_id}:"
                    f"{to_utc(subscription.current_period_started_at).isoformat()}:{allowance_nanos}"
                ),
                kind=CreditKind.Subscription,
                scope=CreditScope.AllMetered,
                amount_nanos=allowance_nanos - issued,
                effective_at=to_utc(effective_at),
                expires_at=to_utc(subscription.current_period_ended_at),
            ),
        )
    BillingAllowanceRepository(session).confirm_credit(
        user_id=user_id,
        period_started_at=subscription.current_period_started_at,
        at=now,
    )
    BillingLedgerRepository(session).settle_pending_credits(owner_user_id=user_id)
    return True


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
        funded = next(
            (
                line
                for line in evidence
                if not line.prorated
                and line.period_started_at == period.started_at
                and line.period_ended_at == period.ended_at
                and (
                    line.plan is BillingPlanId.Free
                    or (line.invoice_paid_nanos > 0 and line.amount_nanos > 0)
                )
            ),
            None,
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
            ),
            plan=funded.plan,
            allowance_nanos=period.allowance_nanos,
        )


__all__ = ["fund_subscription_credits", "initialize_local_credits", "reconcile_credit_cutover"]
