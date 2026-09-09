from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, CheckConstraint, DateTime, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql.schema import SchemaItem

from database.tables.base import DatabaseBase, TimestampMixin, uuid_type


class BillingCreditAdjustmentTable(TimestampMixin, DatabaseBase):
    __tablename__ = "billing_credit_adjustments"
    __table_args__: tuple[SchemaItem, ...] = (
        UniqueConstraint("credit_lot_id", "source_id", name="uq_billing_credit_adjustments_source"),
        CheckConstraint("amount_nanos <> 0", name="ck_billing_credit_adjustments_amount"),
    )

    id: Mapped[str] = mapped_column(uuid_type, primary_key=True)
    credit_lot_id: Mapped[str] = mapped_column(
        uuid_type, ForeignKey("billing_credit_lots.id", ondelete="RESTRICT"), nullable=False
    )
    source_id: Mapped[str] = mapped_column(String(255), nullable=False)
    amount_nanos: Mapped[int] = mapped_column(BigInteger, nullable=False)
    effective_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
