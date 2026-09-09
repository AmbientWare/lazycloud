from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql.schema import SchemaItem

from database.tables.base import DatabaseBase, TimestampMixin, uuid_type


class BillingPreferencesTable(TimestampMixin, DatabaseBase):
    __tablename__ = "billing_preferences"
    __table_args__: tuple[SchemaItem, ...] = (
        CheckConstraint(
            "monthly_usage_limit_nanos IS NULL OR monthly_usage_limit_nanos >= 0",
            name="ck_billing_preferences_usage_limit",
        ),
        CheckConstraint(
            "reload_threshold_cents >= 0 AND reload_amount_cents > 0 AND "
            "(reload_monthly_payment_limit_cents IS NULL OR "
            "reload_monthly_payment_limit_cents >= 0)",
            name="ck_billing_preferences_reload_amounts",
        ),
        CheckConstraint(
            "(reload_paused_purchase_id IS NULL AND reload_pause_reason = '') OR "
            "(reload_paused_purchase_id IS NOT NULL AND "
            "reload_pause_reason IN ('declined', 'action_required'))",
            name="ck_billing_preferences_reload_pause",
        ),
        Index(
            "ix_billing_preferences_reload_due",
            "reload_checked_at",
            "user_id",
            postgresql_where=text("reload_enabled AND reload_paused_purchase_id IS NULL"),
            sqlite_where=text("reload_enabled AND reload_paused_purchase_id IS NULL"),
        ),
    )

    user_id: Mapped[str] = mapped_column(
        uuid_type, ForeignKey("users.id", ondelete="RESTRICT"), primary_key=True
    )
    monthly_usage_limit_nanos: Mapped[int | None] = mapped_column(BigInteger)
    reload_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    reload_threshold_cents: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("1000")
    )
    reload_amount_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    reload_monthly_payment_limit_cents: Mapped[int | None] = mapped_column(BigInteger)
    reload_paused_purchase_id: Mapped[str | None] = mapped_column(
        uuid_type,
        ForeignKey("credit_purchases.id", ondelete="RESTRICT"),
    )
    reload_pause_reason: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default=text("''")
    )
    reload_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reload_resumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


__all__ = ["BillingPreferencesTable"]
