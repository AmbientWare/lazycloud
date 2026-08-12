from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from uuid import uuid4

from database.tables.billing_priced_days import BillingPricedDayTable
from shared.timestamps import utc_now
from sqlalchemy import select
from sqlalchemy.orm import Session


@dataclass(frozen=True, slots=True)
class BillingPricedDayRepository:
    session: Session

    def record(self, *, day: date, workspaces: int, failures: int) -> None:
        """Say what a sweep of one day came to, replacing whatever it said before.

        A day is swept many times — the pricing window reaches back several days
        and runs every hour — and the latest answer is the true one: a day that
        failed for one workspace and succeeded on the next sweep is priced, and a
        day that succeeded and then failed is not.
        """

        row = self.session.scalars(
            select(BillingPricedDayTable).where(BillingPricedDayTable.day == day)
        ).first()
        if row is None:
            row = BillingPricedDayTable(id=str(uuid4()), day=day)
            self.session.add(row)
        row.workspaces = workspaces
        row.failures = failures
        row.completed_at = utc_now()
        self.session.flush()

    def unpriced_days(self, *, start: date, end: date) -> tuple[date, ...]:
        """Which days in a half-open interval are not known to be fully priced.

        Empty means the interval is complete and a month over it can be billed.
        Anything else names exactly what is missing, which is the difference
        between an operator seeing "billing is behind on the 29th and 30th" and
        seeing an invoice that is quietly short.
        """

        complete = set(
            self.session.scalars(
                select(BillingPricedDayTable.day).where(
                    BillingPricedDayTable.day >= start,
                    BillingPricedDayTable.day < end,
                    BillingPricedDayTable.failures == 0,
                )
            ).all()
        )
        span = (end - start).days
        return tuple(
            day
            for day in (start + timedelta(days=offset) for offset in range(span))
            if day not in complete
        )


__all__ = ["BillingPricedDayRepository"]
