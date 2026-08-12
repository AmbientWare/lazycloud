from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from uuid import uuid4

from database.tables.billing_ledger import BillingLedgerEntryTable
from shared.billing import BillableMetric
from shared.billing_ledger import BillingLedgerEntry
from shared.usage import UsageUnit
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session


@dataclass(frozen=True, slots=True)
class BillingLedgerRepository:
    session: Session

    def record_day(
        self,
        *,
        workspace_id: str,
        day: date,
        entries: tuple[BillingLedgerEntry, ...],
    ) -> int:
        """Make a day's rows say exactly what the recomputation says, and nothing else.

        Re-running a day is the normal case: usage arrives late, runs are
        retried, and the sweep reprices the last few days every hour. The day is
        rewritten whole — every row for it removed, then the new set inserted —
        so a second run converges instead of accumulating.

        Written whole rather than merged row by row because the identity a merge
        would key on includes `effective_date`, which is null for a line nothing
        priced. Null is not equal to null, so those rows never conflict with
        themselves and every re-price would insert another copy: an unpriced GPU
        model would grow a row per sweep, forever, and `for_day` would return the
        same line many times over.

        The whole day is replaced in one transaction so no reader sees it
        half-written. Nothing refers to these rows by id — they are a computed
        record of a finished day, not an audit trail — so replacing them costs
        nothing a caller can observe.
        """

        self.session.execute(
            delete(BillingLedgerEntryTable).where(
                BillingLedgerEntryTable.workspace_id == workspace_id,
                BillingLedgerEntryTable.day == day,
            )
        )
        for entry in entries:
            self.session.add(
                BillingLedgerEntryTable(
                    id=str(uuid4()),
                    workspace_id=entry.workspace_id,
                    user_id=entry.user_id,
                    day=entry.day,
                    metric=entry.metric.value,
                    variant=entry.variant,
                    effective_date=entry.effective_date,
                    quantity=entry.quantity,
                    unit=entry.unit.value,
                    price_per_unit_nanos=entry.price_per_unit_nanos,
                    cost_nanos=entry.cost_nanos,
                    currency=entry.currency,
                )
            )
        self.session.flush()
        return len(entries)

    def for_period(
        self,
        *,
        user_id: str,
        period_start: date,
        period_end: date,
    ) -> tuple[BillingLedgerEntry, ...]:
        """Every line this account owes over a period, priced or not.

        Keyed on the payer the line recorded, not on who owns its workspace now:
        a workspace deleted before the month ended takes its membership with it
        and would take the debt too.
        """

        rows = self.session.scalars(
            select(BillingLedgerEntryTable)
            .where(
                BillingLedgerEntryTable.user_id == user_id,
                BillingLedgerEntryTable.day >= period_start,
                BillingLedgerEntryTable.day < period_end,
            )
            .order_by(
                BillingLedgerEntryTable.metric.asc(),
                BillingLedgerEntryTable.variant.asc(),
                BillingLedgerEntryTable.day.asc(),
            )
        ).all()
        return tuple(_entry(row) for row in rows)

    def accrued_since(self, *, user_id: str, since: date) -> int:
        """What this account has run up since a day, across every workspace it owns.

        Read on the compute path, so it is a scalar sum over an indexed range
        rather than the row fetch beside it — the gate asks this question far more
        often than a month is closed.

        Reads finished days only, because a day still accumulating would be
        priced from a partial rollup. So it under-reports, always downward: a
        gate built on it cuts off later than the true figure, never earlier.

        The lag is a little over a day while pricing is keeping up — the current
        day plus however long since the last hourly run, which has yesterday to
        price too. It is unbounded when pricing is behind, and nothing here can
        tell the difference. What that costs depends entirely on what the account
        is running, and nothing caps that, so this is a floor on what an account
        has spent rather than a measure of it.
        """

        total = self.session.scalar(
            select(func.coalesce(func.sum(BillingLedgerEntryTable.cost_nanos), 0)).where(
                BillingLedgerEntryTable.user_id == user_id,
                BillingLedgerEntryTable.day >= since,
            )
        )
        return int(total or 0)

    def payers_for_period(self, *, period_start: date, period_end: date) -> tuple[str, ...]:
        """Every account with priced usage in a period.

        The close sweeps these rather than every user, so a month opens a period
        only where something ran. Enumerating accounts instead would write an
        empty period for everyone who has ever signed up, every month.
        """

        rows = self.session.scalars(
            select(BillingLedgerEntryTable.user_id)
            .where(
                BillingLedgerEntryTable.day >= period_start,
                BillingLedgerEntryTable.day < period_end,
            )
            .group_by(BillingLedgerEntryTable.user_id)
            .order_by(BillingLedgerEntryTable.user_id.asc())
        ).all()
        return tuple(rows)

    def last_payer_for(self, workspace_id: str) -> str | None:
        """Who last owed for this workspace, or nothing if it never owed anything.

        The ledger is the durable record of who pays for a workspace, and it is
        the only one that survives the workspace. Deleting a workspace takes its
        membership with it while its usage rows stay, so the final part-day would
        otherwise be unpriceable — metered, real, and billable to nobody.
        """

        return self.session.scalars(
            select(BillingLedgerEntryTable.user_id)
            .where(BillingLedgerEntryTable.workspace_id == workspace_id)
            .order_by(BillingLedgerEntryTable.day.desc())
            .limit(1)
        ).first()

    def for_day(self, workspace_id: str, day: date) -> tuple[BillingLedgerEntry, ...]:
        rows = self.session.scalars(
            select(BillingLedgerEntryTable)
            .where(
                BillingLedgerEntryTable.workspace_id == workspace_id,
                BillingLedgerEntryTable.day == day,
            )
            .order_by(
                BillingLedgerEntryTable.metric.asc(),
                BillingLedgerEntryTable.variant.asc(),
                BillingLedgerEntryTable.effective_date.asc(),
            )
        ).all()
        return tuple(_entry(row) for row in rows)


def _entry(row: BillingLedgerEntryTable) -> BillingLedgerEntry:
    return BillingLedgerEntry(
        workspace_id=row.workspace_id,
        user_id=row.user_id,
        day=row.day,
        metric=BillableMetric(row.metric),
        variant=row.variant,
        effective_date=row.effective_date,
        quantity=row.quantity,
        unit=UsageUnit(row.unit),
        price_per_unit_nanos=row.price_per_unit_nanos,
        cost_nanos=row.cost_nanos,
        currency=row.currency,
    )


__all__ = ["BillingLedgerRepository"]
