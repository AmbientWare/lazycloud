from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from database.client import DatabaseClient
from database.repositories.billing import BillingAccountRepository
from database.repositories.billing_allowance import BillingAllowanceRepository
from database.repositories.billing_costs import BillingLedgerCostRepository
from database.repositories.billing_outbox import (
    BillingMeterOutboxRepository,
    UndeliveredMeterTotals,
)
from pydantic import JsonValue
from shared.billing_accounts import BillingAccount, BillingAccountStatus
from shared.enums import StringEnum
from shared.events import EventLevel
from shared.payments import METER_EVENT_NAMES, PaymentProvider, ProviderInvoice
from shared.timestamps import to_utc, utc_now

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
"""How far back an invoice is looked for.

Past one monthly cycle plus the days its own invoice takes to finalize, so every
account has exactly one closed period to compare and none has none.
"""

FINALIZED_INVOICE_STATUSES = frozenset({"paid", "open", "uncollectible", "void"})
"""Provider words for an invoice whose lines are settled enough to compare.

A draft is not one: its lines are still being assembled, and comparing against a
total that has not stopped moving would report a disagreement that resolves
itself in an hour.
"""


class BillingDivergence(StringEnum):
    """What the provider and this platform disagree about.

    A closed vocabulary rather than prose because it reaches a durable event an
    operator filters on, and because each value names a different way money goes
    wrong.
    """

    PlanDisagrees = "plan_disagrees"
    SubscriptionEnded = "subscription_ended"
    StandingDisagrees = "standing_disagrees"
    PeriodDisagrees = "period_disagrees"
    MeteredUsageDisagrees = "metered_usage_disagrees"
    UsageNotDelivered = "usage_not_delivered"
    UsageAbandoned = "usage_abandoned"
    """Priced usage the outbox gave up on delivering.

    Its own kind because it is the one difference that never resolves itself: a
    backlog is delivered eventually and the two sides agree again, where this is
    a charge that will not be made until somebody makes it. Both are subtracted
    from the ledger before the arithmetic, so the comparison stays a statement
    about what reached the invoice rather than reporting the same money twice.
    """


@dataclass(frozen=True, slots=True)
class BillingReconciliationResult:
    """What one pass looked at, and how much of it disagreed."""

    accounts_checked: int = 0
    divergent_count: int = 0
    unreachable_count: int = 0


@dataclass(slots=True)
class BillingReconciliationService:
    """Compare what the provider holds against what this platform recorded.

    Reports and never corrects. The plan-change sweep finishes a transaction
    this platform started and wrote a durable intent for, so it knows what was
    meant and may complete it. Nothing here has an intent behind it: a
    difference may be a delivery that never arrived or a deliberate change made
    in the provider's own dashboard, and quietly making the two agree would be a
    money write on a guess. A loud disagreement is the cheaper failure.

    Nothing on this path writes to `billing_accounts`, the allowance periods or
    the ledger, on any branch.
    """

    database: DatabaseClient
    payments: Callable[[], PaymentProvider]
    events: BillingEventSink
    batch_limit: int = 100
    max_accounts: int = 500
    cursor_user_id: str | None = field(default=None, init=False)
    """Where the last pass stopped, `None` at the start of the walk.

    A pass covers a bounded slice and the next one continues from here, so a
    large installation is walked across passes rather than in one unbounded
    read."""

    reported: dict[str, tuple[str, ...]] = field(default_factory=dict, init=False)
    """The divergence each account was last reported with.

    One durable event per account per hour would bury the report it exists to
    make, so an account is reported when what it disagrees about changes and not
    again. In memory deliberately: a restart re-reports each live divergence
    once, which is a repeat rather than a miss, and durable state here would be
    a second record of something the provider and the rows already answer.
    """

    def reconcile(self, *, now: datetime | None = None) -> BillingReconciliationResult:
        """Walk a bounded slice of subscribed accounts and report what differs.

        The credential is resolved first, so a deployment missing it fails the
        whole pass by name rather than per account.

        One account whose provider read fails is counted and stepped over: a
        pass that stopped there would leave every account after it unchecked for
        as long as that one stayed unreachable.
        """

        moment = to_utc(now or utc_now())
        payments = self.payments()
        checked = divergent = unreachable = 0
        while checked < self.max_accounts:
            accounts = self._page()
            if not accounts:
                # The end of the walk, and the next pass starts over from the
                # first account.
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
        self, payments: PaymentProvider, account: BillingAccount, *, now: datetime
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
        data: dict[str, JsonValue] = {
            "user_id": account.user_id,
            "provider_subscription_id": account.provider_subscription_id,
            "plan": account.plan.value if account.plan is not None else "",
            "provider_plan": subscription.plan.value if subscription.plan is not None else "",
            "status": account.status.value,
            "provider_status": subscription.status,
        }
        if subscription.plan is not account.plan:
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
            # The renewal whose delivery never arrived: the provider rolled the
            # cycle and raised an invoice, and no period opened here for the
            # allowance it comes with.
            kinds.add(BillingDivergence.PeriodDisagrees)
        kinds.update(self._usage_divergences(payments, account, data=data, now=now))
        return self._report(account, kinds, data=data)

    def _usage_divergences(
        self,
        payments: PaymentProvider,
        account: BillingAccount,
        *,
        data: dict[str, JsonValue],
        now: datetime,
    ) -> set[BillingDivergence]:
        """Whether the last closed invoice was billed what the ledger holds.

        One invoice per account per pass: the newest whose period has closed
        inside the lookback. Both sides are integer nanodollars — the metered
        prices are one nanodollar per unit and a meter event carries the ledger
        segment's cost verbatim — so a difference is a fact rather than a
        rounding argument.

        What never reached the provider is subtracted before the comparison,
        whether it still can or not. An account with a delivery backlog owes the
        difference rather than disagreeing about it, and one holding an
        abandoned charge is short by it for good; each is said as a divergence
        of its own, which is what keeps three different failures from arriving
        as one number that does not add up.
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
        kinds: set[BillingDivergence] = set()
        meters: dict[str, JsonValue] = {}
        for dimension, event_name in METER_EVENT_NAMES.items():
            ledger_nanos = priced.get(dimension, 0)
            outstanding = undelivered.get(event_name, UndeliveredMeterTotals())
            invoiced_nanos = invoiced.get(event_name, 0)
            figures: dict[str, JsonValue] = {
                "ledger_nanos": ledger_nanos,
                "undelivered_nanos": outstanding.waiting_nanos,
                "abandoned_nanos": outstanding.abandoned_nanos,
                "invoiced_nanos": invoiced_nanos,
            }
            meters[event_name] = figures
            if outstanding.abandoned_nanos:
                kinds.add(BillingDivergence.UsageAbandoned)
            if (
                ledger_nanos - outstanding.waiting_nanos - outstanding.abandoned_nanos
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
        self, payments: PaymentProvider, account: BillingAccount, *, now: datetime
    ) -> ProviderInvoice | None:
        """The newest finalized invoice covering a period there is usage in.

        An invoice whose period is an instant is skipped, and it is not a
        curiosity: a plan change is prorated onto an invoice raised there and
        then, which carries no metered line and covers no span. Taken as the
        newest closed period it would compare an empty window against an empty
        invoice, agree, and leave the account with no usage reconciliation at
        all — starting from the upgrade, which is the event whose lost delivery
        this pass exists to catch.
        """

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
                # A cluster event: an account is a person, not a workspace, and
                # the workspaces behind one are not who the provider bills.
                workspace_id=None,
            )
        except Exception:
            # Wrapped so that failing to record the disagreement cannot replace
            # the disagreement as what this pass reports.
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
