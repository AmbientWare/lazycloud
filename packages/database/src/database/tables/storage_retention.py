from __future__ import annotations

from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, String, text
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql.schema import SchemaItem

from database.tables.base import DatabaseBase, IdTable, uuid_type


class StorageRetentionPeriodTable(IdTable, DatabaseBase):
    __tablename__ = "storage_retention_periods"
    __table_args__: tuple[SchemaItem, ...] = (
        CheckConstraint(
            "ended_at IS NULL OR ended_at >= started_at",
            name="ck_storage_retention_periods_window",
        ),
        Index("ix_storage_retention_periods_account", "user_id", "started_at"),
        Index(
            "uq_storage_retention_periods_open",
            "user_id",
            unique=True,
            postgresql_where=text("ended_at IS NULL"),
            sqlite_where=text("ended_at IS NULL"),
        ),
    )

    user_id: Mapped[str] = mapped_column(
        uuid_type, ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    notification_message_id: Mapped[str] = mapped_column(String(255), nullable=False, default="")
