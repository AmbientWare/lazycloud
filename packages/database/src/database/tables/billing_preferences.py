from sqlalchemy import BigInteger, CheckConstraint, ForeignKey
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
    )

    user_id: Mapped[str] = mapped_column(
        uuid_type, ForeignKey("users.id", ondelete="RESTRICT"), primary_key=True
    )
    monthly_usage_limit_nanos: Mapped[int | None] = mapped_column(BigInteger)


__all__ = ["BillingPreferencesTable"]
