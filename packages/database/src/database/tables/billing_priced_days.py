from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import CheckConstraint, Date, DateTime, Integer, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql.schema import SchemaItem

from database.tables.base import DatabaseBase, IdTable


class BillingPricedDayTable(IdTable, DatabaseBase):
    """That a UTC day has been priced for every workspace that ran something.

    The ledger cannot answer this. A day on which a workspace ran nothing is
    correctly empty, and so is a day nobody ever priced — the absence of rows
    means both, and closing a month on that ambiguity invoices whatever happened
    to be written by then.

    That matters because closing is one-way. A month settled while a day of it
    was still unpriced freezes a total short by that day, and the invoice it
    produces can only be voided, never corrected. So the sweep records what it
    finished, and the close refuses a month with a day missing from this table
    rather than billing an amount it cannot justify.

    `failures` is kept rather than only completeness because a day where one
    workspace could not be priced is not a day that can be billed, and the count
    is what tells an operator whether the gap is one workspace or all of them.
    """

    __tablename__ = "billing_priced_days"
    __table_args__: tuple[SchemaItem, ...] = (
        UniqueConstraint("day", name="uq_billing_priced_days_day"),
        CheckConstraint("workspaces >= 0", name="ck_billing_priced_days_workspaces"),
        CheckConstraint("failures >= 0", name="ck_billing_priced_days_failures"),
    )

    day: Mapped[date] = mapped_column(Date, nullable=False)
    workspaces: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    failures: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    completed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
