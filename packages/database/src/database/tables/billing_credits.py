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


class BillingCreditLotTable(TimestampMixin, DatabaseBase):
    __tablename__ = "billing_credit_lots"
    __table_args__: tuple[SchemaItem, ...] = (
        UniqueConstraint("user_id", "source_id", name="uq_billing_credit_lots_source"),
        CheckConstraint("amount_nanos > 0", name="ck_billing_credit_lots_amount"),
        CheckConstraint(
            "kind IN ('purchased', 'subscription', 'trial')", name="ck_billing_credit_lots_kind"
        ),
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
    amount_nanos: Mapped[int] = mapped_column(BigInteger, nullable=False)
    effective_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class BillingCreditAllocationTable(TimestampMixin, DatabaseBase):
    __tablename__ = "billing_credit_allocations"
    __table_args__: tuple[SchemaItem, ...] = (
        CheckConstraint("amount_nanos > 0", name="ck_billing_credit_allocations_amount"),
        CheckConstraint("ended_at > started_at", name="ck_billing_credit_allocations_window"),
        Index("ix_billing_credit_allocations_segment", "ledger_segment_id"),
        Index("ix_billing_credit_allocations_lot", "credit_lot_id"),
    )

    id: Mapped[str] = mapped_column(uuid_type, primary_key=True)
    credit_lot_id: Mapped[str] = mapped_column(
        uuid_type, ForeignKey("billing_credit_lots.id", ondelete="RESTRICT"), nullable=False
    )
    ledger_segment_id: Mapped[str] = mapped_column(
        uuid_type, ForeignKey("billing_ledger_segments.id", ondelete="RESTRICT"), nullable=False
    )
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ended_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    amount_nanos: Mapped[int] = mapped_column(BigInteger, nullable=False)


class BillingCreditSettlementTable(TimestampMixin, DatabaseBase):
    __tablename__ = "billing_credit_settlements"
    __table_args__: tuple[SchemaItem, ...] = (
        CheckConstraint("gross_nanos >= 0", name="ck_billing_credit_settlements_gross"),
        CheckConstraint(
            "(settled_at IS NULL AND credited_nanos IS NULL AND payable_nanos IS NULL) OR "
            "(settled_at IS NOT NULL AND credited_nanos IS NOT NULL AND payable_nanos IS NOT NULL "
            "AND credited_nanos >= 0 AND payable_nanos >= 0 AND waived_nanos >= 0 "
            "AND credited_nanos + payable_nanos + waived_nanos = gross_nanos)",
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
    waived_nanos: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default="0"
    )
    settled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


__all__ = [
    "BillingCreditAllocationTable",
    "BillingCreditLotTable",
    "BillingCreditSettlementTable",
]
