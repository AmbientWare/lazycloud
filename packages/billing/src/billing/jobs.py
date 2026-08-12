from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from enum import Enum

from database.context import ServiceContext
from database.repositories.billing import BillingAccountRepository
from database.repositories.billing_ledger import BillingLedgerRepository
from database.repositories.billing_periods import BillingPeriodRepository
from database.repositories.billing_priced_days import BillingPricedDayRepository
from database.repositories.usage_billing import UsageBillingRepository
from observability.usage import UsageService
from shared.billing_periods import BillingPeriodStatus
from shared.billing_plans import DEFAULT_BILLING_PLANS
from shared.payments import PaymentProvider
from sqlalchemy.orm import Session

from billing.invoices import COLLECTABLE, BillingInvoiceService, collectable
from billing.ledger import BillingLedgerService, utc_day_bounds
from billing.periods import BillingPeriodService, month_bounds

LOGGER = logging.getLogger(__name__)

PRICING_LOOKBACK_DAYS = 3
"""How far back each run reprices days it has already done.

For usage that arrives after the day it belongs to: a window reported late is
recorded against its own day, and only a repricing of that day picks it up.
Pricing a day is a recomputation that converges, so repeating one costs queries
and never money.

Not what makes a day get priced at all — days nothing finished are recovered by
name over a much longer window, so this can stay short.
"""

PRICING_RECOVERY_DAYS = 45
"""How far back a run will reach for a day it never finished.

The ordinary window is short because it is walked every hour. This one exists for
the case the short window cannot serve: a scheduler down longer than the window
leaves days nothing will ever revisit, and the close then refuses the month
rather than billing it short — correct, but stalled until something prices those
days. Reaching back for exactly the days recorded as unfinished is what unstalls
it, and it costs one query on a system that is not behind.

Long enough to cover a whole month plus the grace, because that is the span a
close can still be waiting on.
"""

CLOSE_GRACE_DAYS = 2
"""How long after a month ends before it is settled.

A month closed the instant it ended would freeze a total that the pricing run for
its last day had not written yet, and the invoice would be short a day
permanently — closing is one-way and the ledger catching up afterwards changes
nothing.

Not the guarantee, though: the close refuses a month with a day nothing finished,
whatever the calendar says. This is what keeps that refusal from being the
ordinary case, by leaving the last day of the month time to be swept before
anything asks to settle it.
"""


def previous_day(now: datetime) -> date:
    """The last day that has finished.

    Never today: a day still accumulating would be priced from a partial rollup
    and rewritten on the next run, and a customer watching would see the figure
    move under them.
    """

    return (now.astimezone(UTC) - timedelta(days=1)).date()


def pricing_days(now: datetime, *, lookback_days: int = PRICING_LOOKBACK_DAYS) -> tuple[date, ...]:
    """The finished days a run prices, newest first.

    A window rather than yesterday alone. Yesterday alone is only correct while
    nothing ever misses a run, and what a missed run costs is not an hour of
    latency but that day's revenue for every customer — nothing else revisits an
    unpriced day.
    """

    last = previous_day(now)
    return tuple(last - timedelta(days=offset) for offset in range(lookback_days))


def closable_month(
    now: datetime, *, grace_days: int = CLOSE_GRACE_DAYS
) -> tuple[date, date] | None:
    """The month that has ended and settled down, or nothing yet.

    Always the previous month once its grace has passed, on any day of the
    current one — not only on the day it becomes closable. A job that settled a
    month solely on one day would lose that month entirely to one missed run.

    Re-running is free: an account already invoiced is skipped, so this converges
    rather than repeating.
    """

    today = now.astimezone(UTC).date()
    this_month, _ = month_bounds(today)
    if today - this_month < timedelta(days=grace_days):
        return None
    return month_bounds(this_month - timedelta(days=1))


@dataclass(frozen=True, slots=True)
class BillingDailyJob:
    """Prices the recently finished days for every workspace that ran something."""

    context: ServiceContext
    usage: UsageService
    lookback_days: int = PRICING_LOOKBACK_DAYS

    def run(self, *, now: datetime) -> bool:
        priced = 0
        failed = 0
        for day in self._days(now):
            day_priced, day_failed = self._price_day(day)
            priced += day_priced
            failed += day_failed
            with self.context.database.session() as session:
                BillingPricedDayRepository(session).record(
                    day=day, workspaces=day_priced, failures=day_failed
                )
                session.commit()
        LOGGER.info("billing: priced %d workspace-days, %d failed", priced, failed)
        # Nothing here is deferred to a later tick: a day is priced whole or its
        # failures are reported, and either way the next run reprices it.
        return True

    def _days(self, now: datetime) -> tuple[date, ...]:
        """The finished days this run prices: the recent ones, and any left over.

        Newest first, so that when a backlog is being worked through the days a
        close is most likely waiting on are done before anything times out.
        """

        recent = pricing_days(now, lookback_days=self.lookback_days)
        last = previous_day(now)
        with self.context.database.session() as session:
            missing = BillingPricedDayRepository(session).unpriced_days(
                start=last - timedelta(days=PRICING_RECOVERY_DAYS),
                end=last + timedelta(days=1),
            )
        return tuple(sorted(set(recent) | set(missing), reverse=True))

    def _price_day(self, day: date) -> tuple[int, int]:
        start, end = utc_day_bounds(day)
        with self.context.database.session() as session:
            workspaces = UsageBillingRepository(session).workspaces_with_usage_between(
                start=start, end=end
            )
        priced = 0
        failed = 0
        for workspace_id in workspaces:
            # One workspace's failure is its own, whatever kind it is. A pricing
            # error is a defect worth seeing, not a reason to leave every later
            # workspace — and every later day — unpriced.
            try:
                overview = self.usage.billing_overview(
                    workspace_id=workspace_id, start=start, end=end, bucket_seconds=86_400
                )
                with self.context.database.session() as session:
                    BillingLedgerService(session).record_day(overview)
                    session.commit()
                priced += 1
            except Exception:
                failed += 1
                LOGGER.exception("billing: pricing %s for %s failed", workspace_id, day.isoformat())
        return priced, failed


_FINISHED = frozenset(
    {
        BillingPeriodStatus.Paid,
        BillingPeriodStatus.NothingOwed,
        BillingPeriodStatus.Voided,
    }
)
"""States with nothing left to do. Deliberately not `Invoiced`: an issued month
is one still owed, and skipping it is how a charge that never completed becomes
usage nobody was ever billed for."""


class _Outcome(Enum):
    """What one account's turn came to."""

    Settled = "settled"
    Skipped = "skipped"
    Failed = "failed"


@dataclass(frozen=True, slots=True)
class BillingCloseJob:
    """Settles the month that has ended for every account that owes anything."""

    context: ServiceContext
    payments: Callable[[], PaymentProvider]
    """Resolved at each run rather than held.

    A deployment with no payment credential still runs this job, and resolving
    here is what makes it say so: it raises naming the missing variable, every
    interval, in the scheduler's log. Holding a provider instead would force the
    caller to decide what to pass when there is none, and the only answers are a
    job that silently does not exist and an adapter that pretends.
    """

    batch: int = 25
    """How many accounts one run settles.

    Bounded because this runs inside the scheduler's own loop and every account
    is several calls to the payment provider: an unbounded sweep holds the tick —
    and with it every user's cron job — for as long as the payment provider takes
    to answer for the whole customer base. A truncated run says so and the next
    tick continues, so the bound costs latency and never coverage.
    """

    def run(self, *, now: datetime) -> bool:
        month = closable_month(now)
        if month is None:
            return True
        period_start, period_end = month
        with self.context.database.session() as session:
            unpriced = BillingPricedDayRepository(session).unpriced_days(
                start=period_start, end=period_end
            )
        if unpriced:
            # Refused rather than billed short. Closing freezes the total and
            # issues a document that can only be voided, so a month with a day
            # nobody priced is one this cannot decide — and saying which days are
            # missing is the difference between an operator fixing it and an
            # invoice quietly costing a customer's usage.
            LOGGER.error(
                "billing: refusing to close %s; %d day(s) unpriced, first %s",
                period_start.isoformat(),
                len(unpriced),
                unpriced[0].isoformat(),
            )
            return True
        with self.context.database.session() as session:
            payers = self._payers(session, period_start=period_start, period_end=period_end)
        settled = 0
        spent = 0
        for user_id in payers:
            if spent >= self.batch:
                LOGGER.info("billing: settled %d accounts, more remain", settled)
                # Only worth coming straight back for if something moved. A batch
                # spent entirely on failures would otherwise re-run every tick,
                # retrying the same accounts against the same fault at the rate
                # the scheduler loops.
                return settled == 0
            outcome = self._settle(user_id=user_id, period_start=period_start, now=now)
            # A skip is one indexed read and does not spend the budget; a failure
            # costs as much as a success and does, or an account failing every
            # time would make the sweep unbounded again.
            if outcome is not _Outcome.Skipped:
                spent += 1
            if outcome is _Outcome.Settled:
                settled += 1
        LOGGER.info("billing: settled %d accounts for %s", settled, period_start.isoformat())
        return True

    def _payers(self, session: Session, *, period_start: date, period_end: date) -> tuple[str, ...]:
        """Who this month bills: everyone who ran something, and every subscriber.

        A subscription is owed whether or not anything ran, so an idle paid
        account is as much a payer as a busy one.
        """

        used = BillingLedgerRepository(session).payers_for_period(
            period_start=period_start, period_end=period_end
        )
        subscribed = BillingAccountRepository(session).users_on_plans(
            tuple(
                config.plan
                for config in DEFAULT_BILLING_PLANS.plans
                if config.monthly_price_nanos > 0
            ),
            existing_before=datetime(period_end.year, period_end.month, period_end.day, tzinfo=UTC),
        )
        finished = BillingPeriodRepository(session).users_finished_with(period_start=period_start)
        # Unfinished accounts first. The sweep is bounded and its order is
        # deterministic, so anything sorted behind a full batch of accounts that
        # need no work is not merely delayed — it is never reached.
        return tuple(
            sorted(set(used) | set(subscribed), key=lambda user_id: (user_id in finished, user_id))
        )

    def _charge(self, *, user_id: str, period_start: date, now: datetime) -> _Outcome:
        """Ask for the money, in its own transaction.

        Separate from issuing so a charge that does not complete leaves a month
        correctly invoiced and uncollected rather than money taken against a
        period that does not say so. The next run finds it in that state and
        comes back here.
        """

        with self.context.database.session() as session:
            BillingInvoiceService(session, self.payments).collect(
                user_id=user_id, period_start=period_start, now=now
            )
            session.commit()
        return _Outcome.Settled

    def _settle(self, *, user_id: str, period_start: date, now: datetime) -> _Outcome:
        try:
            with self.context.database.session() as session:
                periods = BillingPeriodRepository(session)
                settled_already = periods.get(user_id=user_id, period_start=period_start)
                if settled_already is not None and settled_already.status in _FINISHED:
                    return _Outcome.Skipped
                if settled_already is not None and settled_already.status in COLLECTABLE:
                    # Issued but not paid: the invoice exists, so the only step
                    # left is the charge. Reached again on every run until the
                    # money moves, because a charge that did not complete leaves
                    # exactly this state and nothing else revisits it.
                    if not collectable(settled_already, now=now):
                        return _Outcome.Skipped
                    return self._charge(user_id=user_id, period_start=period_start, now=now)
                if settled_already is None or settled_already.status is BillingPeriodStatus.Open:
                    BillingPeriodService(session).close_for_month(user_id=user_id, day=period_start)
                    session.commit()
            with self.context.database.session() as session:
                reserved = BillingInvoiceService(session, self.payments).reserve(
                    user_id=user_id, period_start=period_start
                )
                # Committed before anything is issued, so the invoice this period
                # will be billed through is durable before the provider can
                # report a payment against it.
                session.commit()
            if reserved:
                with self.context.database.session() as session:
                    BillingInvoiceService(session, self.payments).issue(
                        user_id=user_id, period_start=period_start
                    )
                    session.commit()
                # Charged in its own transaction, after the invoice is durably
                # recorded as issued. A crash here leaves a month correctly
                # invoiced and uncollected, which the next run finishes; the
                # reverse would leave money taken against a period that does not
                # say it was.
                return self._charge(user_id=user_id, period_start=period_start, now=now)
            return _Outcome.Settled
        except Exception:
            # Closing is idempotent and issuing adopts what it already sent, so
            # the next run retries this account from wherever it stopped. Every
            # kind of failure is caught: one account's broken payment record is
            # not a reason to leave the rest of the month uninvoiced.
            LOGGER.exception(
                "billing: settling %s for %s failed", user_id, period_start.isoformat()
            )
            return _Outcome.Failed


__all__ = [
    "CLOSE_GRACE_DAYS",
    "PRICING_LOOKBACK_DAYS",
    "BillingCloseJob",
    "BillingDailyJob",
    "closable_month",
    "previous_day",
    "pricing_days",
]
