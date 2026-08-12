from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from uuid import uuid4

from database.tables.billing_ledger import BillingLedgerEntryTable
from database.tables.billing_periods import BillingPeriodTable
from shared.billing_accounts import BillingPlan
from shared.billing_periods import BillingPeriod, BillingPeriodStatus
from shared.errors import ConflictError
from shared.timestamps import to_utc, utc_now
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

_OUTCOMES = frozenset(
    {
        BillingPeriodStatus.Paid,
        BillingPeriodStatus.PaymentFailed,
        BillingPeriodStatus.Voided,
    }
)


@dataclass(frozen=True, slots=True)
class BillingPeriodRepository:
    session: Session

    def open_period(
        self,
        *,
        user_id: str,
        period_start: date,
        period_end: date,
        plan: BillingPlan,
        currency: str,
    ) -> BillingPeriod:
        """The account's period for this month, opening it if nobody has yet.

        Idempotent on (account, month) because whatever runs this is a scheduled
        job that will run again: a second call returns the period the first
        opened rather than starting a rival one.
        """

        row = self._row(user_id=user_id, period_start=period_start)
        if row is None:
            row = BillingPeriodTable(
                id=str(uuid4()),
                user_id=user_id,
                period_start=period_start,
                period_end=period_end,
                status=BillingPeriodStatus.Open.value,
                plan=plan.value,
                currency=currency,
            )
            self.session.add(row)
            try:
                self.session.flush()
            except IntegrityError as exc:
                raise ConflictError(
                    f"billing period already open for {user_id} at {period_start}"
                ) from exc
        return _period(row)

    def usage_cost_for(self, *, user_id: str, period_start: date, period_end: date) -> int:
        """What every workspace this account owns ran up over the period.

        Summed from the ledger through workspace ownership, so a workspace added
        mid-month is included from the day it appears without anything having to
        notice it did.
        """

        total = self.session.scalar(
            select(func.coalesce(func.sum(BillingLedgerEntryTable.cost_nanos), 0)).where(
                BillingLedgerEntryTable.user_id == user_id,
                BillingLedgerEntryTable.day >= period_start,
                BillingLedgerEntryTable.day < period_end,
            )
        )
        return int(total or 0)

    def close_period(
        self,
        *,
        user_id: str,
        period_start: date,
        plan: BillingPlan,
        usage_cost_nanos: int,
        included_cost_nanos: int,
        subscription_cost_nanos: int,
        charged_cost_nanos: int,
    ) -> BillingPeriod:
        """Freeze what this period owes.

        Refuses a period that has moved past closing. Once it has gone to the
        payment provider the numbers are what the provider was told, and a second
        close would silently disagree with an invoice already issued.
        """

        row = self._row(user_id=user_id, period_start=period_start, for_update=True)
        if row is None:
            raise ConflictError(f"no billing period to close for {user_id} at {period_start}")
        status = BillingPeriodStatus(row.status)
        if status is not BillingPeriodStatus.Open:
            raise ConflictError(
                f"billing period for {user_id} at {period_start} is {status.value}, "
                "and what it owes has already been settled"
            )
        row.plan = plan.value
        row.status = BillingPeriodStatus.Closed.value
        row.usage_cost_nanos = usage_cost_nanos
        row.included_cost_nanos = included_cost_nanos
        row.subscription_cost_nanos = subscription_cost_nanos
        row.charged_cost_nanos = charged_cost_nanos
        self.session.flush()
        return _period(row)

    def get_by_provider_invoice(self, provider_invoice_id: str) -> BillingPeriod | None:
        """The period an invoice belongs to.

        The direction a payment outcome arrives from: the provider names the
        invoice, and this is what says which month of which account it settles.
        """

        if not provider_invoice_id:
            return None
        row = self.session.scalars(
            select(BillingPeriodTable).where(
                BillingPeriodTable.provider_invoice_id == provider_invoice_id
            )
        ).first()
        return _period(row) if row is not None else None

    def record_invoice(
        self, *, user_id: str, period_start: date, provider_invoice_id: str
    ) -> BillingPeriod:
        """Name the invoice a closed period is being billed through.

        Written before the invoice is issued rather than after. Issuing is
        several round trips to the provider, and the provider can report the
        payment before the last of them returns — a period that only learned its
        invoice id at the end would not exist to be found when that arrives, and
        the outcome would land on nothing.
        """

        row = self._row(user_id=user_id, period_start=period_start, for_update=True)
        if row is None:
            raise ConflictError(f"no billing period to invoice for {user_id} at {period_start}")
        status = BillingPeriodStatus(row.status)
        if status is not BillingPeriodStatus.Closed:
            raise ConflictError(
                f"billing period for {user_id} at {period_start} is {status.value} "
                "and cannot take an invoice"
            )
        row.provider_invoice_id = provider_invoice_id
        self.session.flush()
        return _period(row)

    def settle(
        self, *, user_id: str, period_start: date, status: BillingPeriodStatus
    ) -> BillingPeriod:
        """Record what became of an issued period.

        One entry point for every outcome, because they arrive from one place and
        out of order: a retried failure can land after the payment that fixed it,
        and each has to know what it may overwrite. Paid is terminal — money that
        arrived does not un-arrive, and a late failure describes an attempt that
        was already superseded.
        """

        if status not in _OUTCOMES:
            raise ConflictError(f"{status.value} is not a payment outcome")
        row = self._row(user_id=user_id, period_start=period_start, for_update=True)
        if row is None:
            raise ConflictError(f"no billing period to settle for {user_id} at {period_start}")
        current = BillingPeriodStatus(row.status)
        if current is BillingPeriodStatus.Paid:
            return _period(row)
        if current not in _OUTCOMES and current is not BillingPeriodStatus.Invoiced:
            raise ConflictError(
                f"billing period for {user_id} at {period_start} is {current.value} "
                "and has no outcome to record"
            )
        row.status = status.value
        if status is BillingPeriodStatus.PaymentFailed:
            # Stamped only on failure: it exists to pace retries, and a period
            # that succeeded or was withdrawn has none to pace.
            row.payment_attempted_at = utc_now()
        self.session.flush()
        return _period(row)

    def outstanding_for(self, *, user_id: str) -> tuple[BillingPeriod, ...]:
        """Every period of this account that is still waiting on money.

        What standing is derived from. An account is behind if any of its months
        is, which is a question about the set and not about whichever outcome
        arrived most recently.
        """

        rows = self.session.scalars(
            select(BillingPeriodTable)
            .where(
                BillingPeriodTable.user_id == user_id,
                BillingPeriodTable.status.in_(
                    (
                        BillingPeriodStatus.Invoiced.value,
                        BillingPeriodStatus.PaymentFailed.value,
                    )
                ),
            )
            .order_by(BillingPeriodTable.period_start.asc())
        ).all()
        return tuple(_period(row) for row in rows)

    def users_finished_with(self, *, period_start: date) -> frozenset[str]:
        """Who has nothing left owing for a month.

        The close sweeps in batches, so which accounts it reaches first decides
        which ones it reaches at all. Ordering the finished ones last is what
        stops a full batch of already-settled accounts from being all a run ever
        looks at, leaving a month that still needs work permanently behind them —
        the order is deterministic, so "behind them" means forever.
        """

        rows = self.session.scalars(
            select(BillingPeriodTable.user_id).where(
                BillingPeriodTable.period_start == period_start,
                BillingPeriodTable.status.in_(
                    (
                        BillingPeriodStatus.Paid.value,
                        BillingPeriodStatus.NothingOwed.value,
                        BillingPeriodStatus.Voided.value,
                    )
                ),
            )
        ).all()
        return frozenset(rows)

    def get(self, *, user_id: str, period_start: date) -> BillingPeriod | None:
        row = self._row(user_id=user_id, period_start=period_start)
        return _period(row) if row is not None else None

    def mark_invoiced(
        self,
        *,
        user_id: str,
        period_start: date,
        provider_invoice_id: str,
    ) -> BillingPeriod:
        """Record that an invoice was issued for this period.

        Only a closed period can be invoiced: what an invoice states has to be
        the frozen figure, not one still moving.
        """

        row = self._row(user_id=user_id, period_start=period_start, for_update=True)
        if row is None:
            raise ConflictError(f"no billing period to invoice for {user_id} at {period_start}")
        status = BillingPeriodStatus(row.status)
        if status is not BillingPeriodStatus.Closed:
            raise ConflictError(
                f"billing period for {user_id} at {period_start} is {status.value} "
                "and cannot be invoiced"
            )
        row.status = BillingPeriodStatus.Invoiced.value
        row.provider_invoice_id = provider_invoice_id
        self.session.flush()
        return _period(row)

    def mark_nothing_owed(self, *, user_id: str, period_start: date) -> BillingPeriod:
        """Record that this period finished owing nothing.

        Terminal like `mark_invoiced`, and for the same reason: a closed period
        with no way to finish is one the close sweep reconsiders on every run,
        spending its bounded budget on accounts there is nothing left to do for.
        """

        row = self._row(user_id=user_id, period_start=period_start, for_update=True)
        if row is None:
            raise ConflictError(f"no billing period to settle for {user_id} at {period_start}")
        status = BillingPeriodStatus(row.status)
        if status is not BillingPeriodStatus.Closed:
            raise ConflictError(
                f"billing period for {user_id} at {period_start} is {status.value} "
                "and cannot be settled"
            )
        row.status = BillingPeriodStatus.NothingOwed.value
        self.session.flush()
        return _period(row)

    def _row(
        self, *, user_id: str, period_start: date, for_update: bool = False
    ) -> BillingPeriodTable | None:
        statement = select(BillingPeriodTable).where(
            BillingPeriodTable.user_id == user_id,
            BillingPeriodTable.period_start == period_start,
        )
        if for_update:
            # Two monthly closes racing would both read Open and both write.
            statement = statement.with_for_update()
        return self.session.scalars(statement).first()


def _period(row: BillingPeriodTable) -> BillingPeriod:
    return BillingPeriod(
        id=row.id,
        user_id=row.user_id,
        period_start=row.period_start,
        period_end=row.period_end,
        status=BillingPeriodStatus(row.status),
        plan=BillingPlan(row.plan),
        currency=row.currency,
        usage_cost_nanos=row.usage_cost_nanos,
        included_cost_nanos=row.included_cost_nanos,
        subscription_cost_nanos=row.subscription_cost_nanos,
        charged_cost_nanos=row.charged_cost_nanos,
        provider_invoice_id=row.provider_invoice_id,
        payment_attempted_at=to_utc(row.payment_attempted_at)
        if row.payment_attempted_at is not None
        else None,
    )


__all__ = ["BillingPeriodRepository"]
