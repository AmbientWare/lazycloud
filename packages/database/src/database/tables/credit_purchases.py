from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql.schema import SchemaItem

from database.tables.base import DatabaseBase, TimestampMixin, uuid_type


class CreditPurchaseTable(TimestampMixin, DatabaseBase):
    __tablename__ = "credit_purchases"
    __table_args__: tuple[SchemaItem, ...] = (
        UniqueConstraint("user_id", "request_key", name="uq_credit_purchases_request"),
        UniqueConstraint("provider_session_id", name="uq_credit_purchases_session"),
        UniqueConstraint("provider_payment_id", name="uq_credit_purchases_payment"),
        UniqueConstraint("credit_lot_id", name="uq_credit_purchases_lot"),
        CheckConstraint("amount_nanos > 0", name="ck_credit_purchases_amount"),
        CheckConstraint("kind IN ('manual', 'automatic')", name="ck_credit_purchases_kind"),
        CheckConstraint(
            "status IN ('pending', 'action_required', 'succeeded', 'declined', 'cancelled')",
            name="ck_credit_purchases_status",
        ),
        CheckConstraint(
            "reversed_nanos >= 0 AND reversed_nanos <= amount_nanos",
            name="ck_credit_purchases_reversal",
        ),
        Index("ix_credit_purchases_reconciliation", "status", "updated_at"),
        Index(
            "uq_credit_purchases_pending_automatic",
            "user_id",
            unique=True,
            postgresql_where=text(
                "kind = 'automatic' AND status IN ('pending', 'action_required')"
            ),
            sqlite_where=text("kind = 'automatic' AND status IN ('pending', 'action_required')"),
        ),
    )

    id: Mapped[str] = mapped_column(uuid_type, primary_key=True)
    user_id: Mapped[str] = mapped_column(
        uuid_type, ForeignKey("users.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    request_key: Mapped[str] = mapped_column(uuid_type, nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    amount_nanos: Mapped[int] = mapped_column(BigInteger, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    provider_customer_id: Mapped[str] = mapped_column(String(255), nullable=False)
    provider_session_id: Mapped[str | None] = mapped_column(String(255))
    provider_payment_id: Mapped[str | None] = mapped_column(String(255))
    success_url: Mapped[str] = mapped_column(String(2048), nullable=False)
    cancel_url: Mapped[str] = mapped_column(String(2048), nullable=False)
    hosted_url: Mapped[str | None] = mapped_column(String(2048))
    session_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    creation_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    credit_lot_id: Mapped[str | None] = mapped_column(
        uuid_type, ForeignKey("billing_credit_lots.id", ondelete="RESTRICT")
    )
    funded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reversed_nanos: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    reversal_sequence: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_error: Mapped[str] = mapped_column(String(255), nullable=False, default="")
