from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from database.client import DatabaseClient
from database.repositories.billing import BillingAccountRepository
from database.repositories.billing_allowance import BillingAllowanceRepository
from database.repositories.billing_costs import BillingLedgerCostRepository
from database.repositories.billing_credits import BillingCreditRepository, CreditAdjustments
from database.repositories.billing_outbox import (
    BillingMeterOutboxRepository,
    UndeliveredMeterTotals,
)
from pydantic import JsonValue
from shared.billing_accounts import BillingAccount, BillingAccountStatus
from shared.enums import StringEnum
from shared.errors import UpstreamUnavailableError
from shared.events import EventLevel
from shared.payments import METER_EVENT_NAMES, ProviderInvoice, SubscriptionPaymentProvider
from shared.timestamps import to_utc, utc_now

from billing.credits import recover_subscription_credits
from billing.periods import carry_plan_into_cycle
from billing.sweeps import BillingEventSink
from billing.webhooks import (
    ENDED_SUBSCRIPTION_STATUSES,
    RUNNING_SUBSCRIPTION_STATUSES,
    UNPAID_SUBSCRIPTION_STATUSES,
)

LOGGER = logging.getLogger(__name__)

RECONCILIATION_DIVERGENCE_ACTION = "billing.reconciliation.divergence"
"""What the provider holds and what this platform recorded do not agree."""

BILLING_ACCOUNT_RESOURCE_TYPE = "billing_account"

INVOICE_LOOKBACK = timedelta(days=45)
# Include a monthly cycle and its invoice finalization delay.

FINALIZED_INVOICE_STATUSES = frozenset({"paid", "open", "uncollectible", "void"})


class BillingDivergence(StringEnum):
    """Stable divergence kinds recorded in billing events."""

    PlanDisagrees = "plan_disagrees"
    SubscriptionEnded = "subscription_ended"
    StandingDisagrees = "standing_disagrees"
    PeriodDisagrees = "period_disagrees"
    MeteredUsageDisagrees = "metered_usage_disagrees"
    UsageNotDelivered = "usage_not_delivered"
    CreditSettlementPending = "credit_settlement_pending"
    UsageAbandoned = "usage_abandoned"
    """Priced usage whose meter export requires operator recovery."""


@dataclass(frozen=True, slots=True)
class BillingReconciliationResult:
    accounts_checked: int = 0
    divergent_count: int = 0
    unreachable_count: int = 0


@dataclass(slots=True)
class BillingReconciliationService:
    """Compare what the provider holds against what this platform recorded.

    Retries local credit funding supported by paid
    invoices. Other plan and standing differences are reported for their owners
    to resolve. Gross ledger history is never rewritten.
    """

    database: DatabaseClient
    payments: Callable[[], SubscriptionPaymentProvider]
    events: BillingEventSink
    batch_limit: int = 100
    max_accounts: int = 500
    cursor_user_id: str | None = field(default=None, init=False)

    reported: dict[str, tuple[str, ...]] = field(default_factory=dict, init=False)
    """Suppress repeated events until divergence changes or this process restarts."""

    def reconcile(self, *, now: datetime | None = None) -> BillingReconciliationResult:
        """Reconcile a bounded page sequence, counting unreachable accounts separately."""

        moment = to_utc(now or utc_now())
        payments = self.payments()
        checked = divergent = unreachable = 0
        while checked < self.max_accounts:
            accounts = self._page()
            if not accounts:
                self.cursor_user_id = None
                break
            for account in accounts:
                self.cursor_user_id = account.user_id
                checked += 1
                kinds = self._divergences(payments, account, now=moment)
                if kinds is None:
                    unreachable += 1
                    continue
                if kinds:
                    divergent += 1
                if checked >= self.max_accounts:
                    break
        if divergent:
            LOGGER.warning(
                "billing: %d of %d reconciled accounts disagree with the provider",
                divergent,
                checked,
            )
        return BillingReconciliationResult(
            accounts_checked=checked,
            divergent_count=divergent,
            unreachable_count=unreachable,
        )

    def _page(self) -> tuple[BillingAccount, ...]:
        with self.database.session() as session:
            return BillingAccountRepository(session).page_subscribed(
                after_user_id=self.cursor_user_id,
                limit=self.batch_limit,
            )

    def _divergences(
        self, payments: SubscriptionPaymentProvider, account: BillingAccount, *, now: datetime
    ) -> tuple[str, ...] | None:
        """What this account disagrees about, or `None` if it could not be read."""

        try:
            subscription = payments.subscription(
                provider_subscription_id=account.provider_subscription_id
            )
        except Exception as error:
            LOGGER.warning(
                "billing: the provider would not answer for %s: %s", account.user_id, error
            )
            return None
        kinds: set[BillingDivergence] = set()
        if (
            subscription.terms_version is not None
            and subscription.status in RUNNING_SUBSCRIPTION_STATUSES
        ):
            with self.database.session() as session:
                accounts = BillingAccountRepository(session)
                held = accounts.get_by_user(account.user_id, for_update=True)
                if (
                    held is not None
                    and held.provider_subscription_id == subscription.provider_subscription_id
                    and held.provider_customer_id == account.provider_customer_id
                    and (
                        (
                            held.plan is subscription.plan
                            and held.subscription_terms_version
                            in {None, subscription.terms_version}
                        )
                        or (
                            held.scheduled_terms_version is subscription.terms_version
                            and held.scheduled_change_at is not None
                            and subscription.current_period_started_at >= held.scheduled_change_at
                        )
                    )
                ):
                    account = accounts.upsert(
                        user_id=held.user_id,
                        status=held.status,
                        provider_customer_id=held.provider_customer_id,
                        provider_subscription_id=held.provider_subscription_id,
                        plan=subscription.plan,
                        subscription_terms_version=subscription.terms_version,
                        scheduled_terms_version=subscription.scheduled_terms_version,
                        scheduled_change_at=subscription.scheduled_change_at,
                    )
        data: dict[str, JsonValue] = {
            "user_id": account.user_id,
            "provider_subscription_id": account.provider_subscription_id,
            "plan": account.plan.value if account.plan is not None else "",
            "provider_plan": subscription.plan.value if subscription.plan is not None else "",
            "status": account.status.value,
            "provider_status": subscription.status,
        }
        try:
            with self.database.session() as session:
                recover_subscription_credits(
                    session, payments, account=account, subscription=subscription
                )
                if (
                    subscription.plan is not None
                    and subscription.plan is account.plan
                    and subscription.terms_version is account.subscription_terms_version
                    and subscription.status in RUNNING_SUBSCRIPTION_STATUSES
                ):
                    try:
                        carry_plan_into_cycle(
                            session,
                            payments,
                            account_id=account.user_id,
                            provider_customer_id=account.provider_customer_id,
                            subscription=subscription,
                            plan=subscription.plan,
                        )
                    except UpstreamUnavailableError:
                        kinds.add(BillingDivergence.CreditSettlementPending)
                        data["credit_funding_pending"] = True
        except Exception as error:
            LOGGER.warning(
                "billing: credit reconciliation failed for %s: %s", account.user_id, error
            )
            return None
        if (
            subscription.plan is not account.plan
            or subscription.terms_version is not account.subscription_terms_version
        ):
            kinds.add(BillingDivergence.PlanDisagrees)
        if subscription.status in ENDED_SUBSCRIPTION_STATUSES:
            kinds.add(BillingDivergence.SubscriptionEnded)
        if (
            subscription.status in UNPAID_SUBSCRIPTION_STATUSES
            and account.status is BillingAccountStatus.Active
        ) or (
            subscription.status in RUNNING_SUBSCRIPTION_STATUSES
            and account.status is BillingAccountStatus.PastDue
        ):
            kinds.add(BillingDivergence.StandingDisagrees)
        with self.database.session() as session:
            period = BillingAllowanceRepository(session).current_period(
                user_id=account.user_id, at=now
            )
        data["period_started_at"] = period.started_at.isoformat() if period else ""
        data["period_ended_at"] = period.ended_at.isoformat() if period else ""
        data["provider_period_started_at"] = subscription.current_period_started_at.isoformat()
        data["provider_period_ended_at"] = subscription.current_period_ended_at.isoformat()
        if (
            period is None
            or period.started_at != subscription.current_period_started_at
            or period.ended_at != subscription.current_period_ended_at
        ):
            kinds.add(BillingDivergence.PeriodDisagrees)
        kinds.update(self._usage_divergences(payments, account, data=data, now=now))
        return self._report(account, kinds, data=data)

    def _usage_divergences(
        self,
        payments: SubscriptionPaymentProvider,
        account: BillingAccount,
        *,
        data: dict[str, JsonValue],
        now: datetime,
    ) -> set[BillingDivergence]:
        """Compare the latest closed invoice against exported ledger cost.

        Remove local wallet charges, waivers and undelivered exports before
        comparing. Report pending and abandoned exports separately.
        """

        invoice = self._closed_invoice(payments, account, now=now)
        if invoice is None:
            return set()
        data["provider_invoice_id"] = invoice.provider_invoice_id
        data["invoice_period_started_at"] = invoice.period_started_at.isoformat()
        data["invoice_period_ended_at"] = invoice.period_ended_at.isoformat()
        try:
            invoiced = payments.invoice_metered_totals(
                provider_invoice_id=invoice.provider_invoice_id
            )
        except Exception as error:
            LOGGER.warning(
                "billing: invoice %s could not be read: %s", invoice.provider_invoice_id, error
            )
            return set()
        with self.database.session() as session:
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
            credit_adjustments = BillingCreditRepository(session).account_adjustments(
                user_id=account.user_id,
                start=invoice.period_started_at,
                end=invoice.period_ended_at,
            )
        kinds: set[BillingDivergence] = set()
        meters: dict[str, JsonValue] = {}
        for dimension, event_name in METER_EVENT_NAMES.items():
            ledger_nanos = priced.get(dimension, 0)
            credits = credit_adjustments.get(dimension, CreditAdjustments())
            outstanding = undelivered.get(event_name, UndeliveredMeterTotals())
            invoiced_nanos = invoiced.get(event_name, 0)
            figures: dict[str, JsonValue] = {
                "ledger_nanos": ledger_nanos,
                "credited_nanos": credits.credited_nanos,
                "unsettled_nanos": credits.unsettled_nanos,
                "wallet_unpaid_nanos": credits.unpaid_nanos,
                "undelivered_nanos": outstanding.waiting_nanos,
                "abandoned_nanos": outstanding.abandoned_nanos,
                "waived_nanos": outstanding.waived_nanos + credits.waived_nanos,
                "invoiced_nanos": invoiced_nanos,
            }
            meters[event_name] = figures
            if credits.unsettled_nanos:
                kinds.add(BillingDivergence.CreditSettlementPending)
            if outstanding.abandoned_nanos:
                kinds.add(BillingDivergence.UsageAbandoned)
            if (
                ledger_nanos
                - credits.credited_nanos
                - credits.unsettled_nanos
                - credits.unpaid_nanos
                - credits.waived_nanos
                - outstanding.waiting_nanos
                - outstanding.abandoned_nanos
                - outstanding.waived_nanos
                != invoiced_nanos
            ):
                kinds.add(
                    BillingDivergence.UsageNotDelivered
                    if outstanding.waiting_nanos
                    else BillingDivergence.MeteredUsageDisagrees
                )
        data["meters"] = meters
        return kinds

    def _closed_invoice(
        self, payments: SubscriptionPaymentProvider, account: BillingAccount, *, now: datetime
    ) -> ProviderInvoice | None:
        """Select the latest finalized period, excluding instant proration invoices."""

        try:
            invoices = payments.invoices_for(
                provider_customer_id=account.provider_customer_id,
                since=now - INVOICE_LOOKBACK,
            )
        except Exception as error:
            LOGGER.warning(
                "billing: the provider would not list invoices for %s: %s",
                account.user_id,
                error,
            )
            return None
        closed = [
            invoice
            for invoice in invoices
            if invoice.period_ended_at <= now
            and invoice.period_started_at < invoice.period_ended_at
            and invoice.status in FINALIZED_INVOICE_STATUSES
        ]
        return max(closed, key=lambda invoice: invoice.period_ended_at) if closed else None

    def _report(
        self,
        account: BillingAccount,
        kinds: set[BillingDivergence],
        *,
        data: dict[str, JsonValue],
    ) -> tuple[str, ...]:
        signature = tuple(sorted(kind.value for kind in kinds))
        if not signature:
            self.reported.pop(account.user_id, None)
            return signature
        if self.reported.get(account.user_id) == signature:
            return signature
        self.reported[account.user_id] = signature
        try:
            self.events.emit(
                RECONCILIATION_DIVERGENCE_ACTION,
                resource_type=BILLING_ACCOUNT_RESOURCE_TYPE,
                resource_id=account.user_id,
                message=(
                    f"the payment provider and this platform disagree about {', '.join(signature)}"
                ),
                level=EventLevel.Error,
                data={**data, "divergences": list(signature)},
                workspace_id=None,
            )
        except Exception:
            # Event delivery failure must not hide the reconciliation result.
            LOGGER.exception(
                "billing: %s disagrees with the provider and the event was not recorded",
                account.user_id,
            )
        return signature


__all__ = [
    "BILLING_ACCOUNT_RESOURCE_TYPE",
    "INVOICE_LOOKBACK",
    "RECONCILIATION_DIVERGENCE_ACTION",
    "BillingDivergence",
    "BillingReconciliationResult",
    "BillingReconciliationService",
]
