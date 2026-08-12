from __future__ import annotations

from datetime import date

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Date,
    Float,
    ForeignKey,
    Index,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql.schema import SchemaItem

from database.tables.base import DatabaseBase, IdTable, uuid_type


class BillingLedgerEntryTable(IdTable, DatabaseBase):
    """What one workspace owes for one metric on one day.

    The priced record an invoice is built from, written once per day from the
    same rollup the dashboard reads, so the two can never disagree about what a
    month cost.

    A day is a whole UTC day and a metering window belongs to whichever day its
    `billing_at` falls in — windows are never split at midnight. The container
    monitor emits one every few seconds (`ContainerRuntimeMonitorSettings`
    defaults to 5, the agent sets 3), so a window straddling the boundary moves
    seconds between two adjacent days and none of it is lost. Splitting would
    need proportional allocation here and in `aggregates()` both, kept in
    agreement forever, to buy that back.

    This holds only while windows stay short. A code path that emitted one window
    covering a container's whole life would put hours on the day it started, and
    the choice would have to be made again.
    """

    __tablename__ = "billing_ledger_entries"
    __table_args__: tuple[SchemaItem, ...] = (
        # What makes a line: one metric, one variant, one rate, on one day.
        # `variant` because GPU seconds price per model, so without it nine models
        # would be one line; `effective_date` for the same reason one rate up, a
        # rate change splitting a metric into two priced lines. Enforced here and
        # not only in the writer because two writers pricing one day concurrently
        # is the case the writer cannot see, and the loser must fail rather than
        # double the day.
        #
        # Null `effective_date` is outside its reach — nulls are distinct — which
        # is why a day is rewritten whole rather than merged row by row.
        UniqueConstraint(
            "workspace_id",
            "day",
            "metric",
            "variant",
            "effective_date",
            name="uq_billing_ledger_entries_day_metric",
        ),
        # The payment gate totals an account's spend on the compute path, which
        # asks far more often than anything else here writes. Without this the
        # sum is a sequential scan of every line the platform has ever priced.
        Index("ix_billing_ledger_entries_payer_day", "user_id", "day"),
        CheckConstraint("quantity >= 0", name="ck_billing_ledger_entries_quantity"),
        CheckConstraint("cost_nanos >= 0", name="ck_billing_ledger_entries_cost"),
        # An unpriced line costs nothing by definition. Without this the two
        # states the null is here to separate can be written as one row that
        # claims a cost nobody priced.
        CheckConstraint(
            "price_per_unit_nanos IS NOT NULL OR cost_nanos = 0",
            name="ck_billing_ledger_entries_unpriced_is_free",
        ),
    )

    workspace_id: Mapped[str] = mapped_column(uuid_type, nullable=False)
    """No foreign key, deliberately. A workspace is a resource a customer can
    delete; what it owed is not. The column keeps naming the workspace after it
    is gone, which is what makes this a record of the month rather than a view
    of what still exists."""

    user_id: Mapped[str] = mapped_column(
        uuid_type,
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
    )
    """Who owes it, resolved when the day was priced.

    Stored rather than joined through workspace membership at invoice time,
    because membership does not survive deleting the workspace and the debt
    does. A line whose payer had to be looked up later would silently total to
    nothing.
    """
    day: Mapped[date] = mapped_column(Date, nullable=False)
    metric: Mapped[str] = mapped_column(String(64), nullable=False)
    variant: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    effective_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    """When the rate that priced this line took effect, null where nothing priced
    it. Part of the identity, because two rates for one metric in one day are two
    lines and not one."""

    quantity: Mapped[float] = mapped_column(Float, nullable=False, default=0)
    unit: Mapped[str] = mapped_column(String(32), nullable=False)
    price_per_unit_nanos: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    """Null where nothing priced this line, which is not the same as free.

    An unpriced GPU model bills nothing and has to stay visible as a gap; a rate
    of zero is a dimension we metered and chose not to charge for. Collapsing the
    two would hide the first behind the second.
    """

    cost_nanos: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
