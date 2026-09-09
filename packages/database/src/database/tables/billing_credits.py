from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql.schema import SchemaItem

from database.tables.base import DatabaseBase, TimestampMixin, uuid_type


class BillingCreditCutoverTable(TimestampMixin, DatabaseBase):
    __tablename__ = "billing_credit_cutovers"
    __table_args__: tuple[SchemaItem, ...] = (
        CheckConstraint(
            "completed_at IS NULL OR completed_at >= effective_at",
            name="ck_billing_credit_cutovers_completion",
        ),
    )

    user_id: Mapped[str] = mapped_column(
        uuid_type, ForeignKey("users.id", ondelete="RESTRICT"), primary_key=True
    )
    effective_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    blocked_reason: Mapped[str] = mapped_column(String(1024), nullable=False, default="")


class BillingCreditLotTable(TimestampMixin, DatabaseBase):
    __tablename__ = "billing_credit_lots"
    __table_args__: tuple[SchemaItem, ...] = (
        UniqueConstraint("user_id", "source_id", name="uq_billing_credit_lots_source"),
        CheckConstraint("amount_nanos > 0", name="ck_billing_credit_lots_amount"),
        CheckConstraint(
            "kind IN ('purchased', 'subscription', 'trial')", name="ck_billing_credit_lots_kind"
        ),
        CheckConstraint("scope IN ('compute', 'all_metered')", name="ck_billing_credit_lots_scope"),
        CheckConstraint(
            "expires_at IS NULL OR expires_at > effective_at", name="ck_billing_credit_lots_expiry"
        ),
        CheckConstraint(
            "kind <> 'purchased' OR expires_at IS NULL", name="ck_billing_credit_lots_purchased"
        ),
        Index("ix_billing_credit_lots_account", "user_id", "effective_at", "expires_at"),
    )

    id: Mapped[str] = mapped_column(uuid_type, primary_key=True)
    user_id: Mapped[str] = mapped_column(
        uuid_type, ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    source_id: Mapped[str] = mapped_column(String(255), nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    scope: Mapped[str] = mapped_column(String(32), nullable=False)
    amount_nanos: Mapped[int] = mapped_column(BigInteger, nullable=False)
    effective_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class BillingCreditAllocationTable(TimestampMixin, DatabaseBase):
    __tablename__ = "billing_credit_allocations"
    __table_args__: tuple[SchemaItem, ...] = (
        CheckConstraint("amount_nanos > 0", name="ck_billing_credit_allocations_amount"),
        CheckConstraint("ended_at > started_at", name="ck_billing_credit_allocations_window"),
        Index("ix_billing_credit_allocations_segment", "ledger_segment_id"),
    )

    credit_lot_id: Mapped[str] = mapped_column(
        uuid_type, ForeignKey("billing_credit_lots.id", ondelete="RESTRICT"), primary_key=True
    )
    ledger_segment_id: Mapped[str] = mapped_column(
        uuid_type, ForeignKey("billing_ledger_segments.id", ondelete="RESTRICT"), primary_key=True
    )
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True)
    ended_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    amount_nanos: Mapped[int] = mapped_column(BigInteger, nullable=False)


class BillingCreditSettlementTable(TimestampMixin, DatabaseBase):
    __tablename__ = "billing_credit_settlements"
    __table_args__: tuple[SchemaItem, ...] = (
        CheckConstraint("gross_nanos >= 0", name="ck_billing_credit_settlements_gross"),
        CheckConstraint(
            "(settled_at IS NULL AND credited_nanos IS NULL AND payable_nanos IS NULL) OR "
            "(settled_at IS NOT NULL AND credited_nanos IS NOT NULL AND payable_nanos IS NOT NULL "
            "AND credited_nanos >= 0 AND payable_nanos >= 0 "
            "AND credited_nanos + payable_nanos = gross_nanos)",
            name="ck_billing_credit_settlements_amounts",
        ),
        Index("ix_billing_credit_settlements_pending", "user_id", "settled_at"),
    )

    usage_record_id: Mapped[str] = mapped_column(uuid_type, primary_key=True)
    user_id: Mapped[str] = mapped_column(
        uuid_type, ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    gross_nanos: Mapped[int] = mapped_column(BigInteger, nullable=False)
    credited_nanos: Mapped[int | None] = mapped_column(BigInteger)
    payable_nanos: Mapped[int | None] = mapped_column(BigInteger)
    settled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


__all__ = [
    "BillingCreditAllocationTable",
    "BillingCreditCutoverTable",
    "BillingCreditLotTable",
    "BillingCreditSettlementTable",
]
