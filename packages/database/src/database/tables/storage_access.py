from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, CheckConstraint, DateTime, ForeignKey, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql.schema import SchemaItem

from database.tables.base import DatabaseBase, uuid_type


class StorageAccessTable(DatabaseBase):
    __tablename__ = "storage_access_observations"
    __table_args__: tuple[SchemaItem, ...] = (
        CheckConstraint(
            "response_bytes IS NULL OR response_bytes >= 0", name="ck_storage_access_bytes"
        ),
        CheckConstraint("status_code BETWEEN 100 AND 599", name="ck_storage_access_status"),
        CheckConstraint(
            "request_class IN ('read','write','delete','other')", name="ck_storage_access_class"
        ),
        CheckConstraint(
            "transfer_evidence IN ('same_region','other_region','unknown')",
            name="ck_storage_access_transfer",
        ),
        Index("ix_storage_access_occurred", "occurred_at"),
        Index("ix_storage_access_workspace_occurred", "workspace_id", "occurred_at"),
    )

    id: Mapped[str] = mapped_column(uuid_type, primary_key=True)
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    bucket: Mapped[str] = mapped_column(String(255), nullable=False)
    request_id: Mapped[str] = mapped_column(String(128), nullable=False)
    operation: Mapped[str] = mapped_column(String(64), nullable=False)
    request_class: Mapped[str] = mapped_column(String(16), nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status_code: Mapped[int] = mapped_column(Integer, nullable=False)
    response_bytes: Mapped[int | None] = mapped_column(BigInteger)
    source_region: Mapped[str] = mapped_column(String(64), nullable=False)
    transfer_evidence: Mapped[str] = mapped_column(String(16), nullable=False)
    workspace_id: Mapped[str | None] = mapped_column(
        uuid_type, ForeignKey("workspaces.id", ondelete="RESTRICT")
    )
