from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql.schema import SchemaItem

from database.tables.base import DatabaseBase, TimestampMixin, uuid_type


class BillingAllowancePeriodTable(TimestampMixin, DatabaseBase):
    """What one account has spent this period, and what it came with.

    Maintained incrementally in the pricing transaction so the customer can be
    shown, in one indexed read, how much of what their plan includes is gone —
    without a provider round trip, and long before an invoice exists to ask.

    Boundaries are explicit `timestamptz` values a caller computed, never
    `date_trunc` or `func.date`: those read the session `TimeZone`, which nothing
    here sets, so the month a cost landed in would depend on the connection that
    happened to write it.
    """

    __tablename__ = "billing_allowance_periods"
    __table_args__: tuple[SchemaItem, ...] = (
        UniqueConstraint(
            "user_id",
            "period_started_at",
            name="uq_billing_allowance_periods_period",
        ),
        CheckConstraint(
            "period_ended_at > period_started_at",
            name="ck_billing_allowance_periods_window",
        ),
        CheckConstraint(
            "spent_nanos >= 0 AND allowance_nanos >= 0",
            name="ck_billing_allowance_periods_nonnegative",
        ),
        Index(
            "ix_billing_allowance_periods_lookup",
            "user_id",
            "period_started_at",
            "period_ended_at",
        ),
    )

    id: Mapped[str] = mapped_column(
        uuid_type,
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    user_id: Mapped[str] = mapped_column(
        uuid_type,
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
    )
    period_started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    period_ended_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    allowance_nanos: Mapped[int] = mapped_column(BigInteger, nullable=False)
    """The plan's included figure, stamped here rather than re-read from the plan.
    Republishing what a plan includes must not restate the terms of a period a
    customer is part-way through; only a plan change does, and it writes here."""
    spent_nanos: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)


__all__ = ["BillingAllowancePeriodTable"]
