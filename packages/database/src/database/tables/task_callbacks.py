from datetime import datetime

from pydantic import JsonValue
from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql.schema import SchemaItem

from database.tables.base import DatabaseBase, IdTable, json_type, uuid_type


class TaskCallbackTable(IdTable, DatabaseBase):
    __tablename__ = "task_callback_outbox"
    __table_args__: tuple[SchemaItem, ...] = (
        CheckConstraint(
            "status IN ('pending', 'sending', 'sent', 'failed')", name="ck_task_callback_status"
        ),
        CheckConstraint("attempts >= 0", name="ck_task_callback_attempts"),
        CheckConstraint(
            "(status = 'sending') = (claim_token IS NOT NULL)", name="ck_task_callback_claim"
        ),
        Index("uq_task_callback_identity", "idempotency_key", unique=True),
        Index("ix_task_callback_ready", "status", "next_attempt_at"),
        Index("ix_task_callback_stale", "status", "claimed_at"),
    )

    task_id: Mapped[str] = mapped_column(
        uuid_type, ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False
    )
    workspace_id: Mapped[str] = mapped_column(
        uuid_type, ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False
    )
    idempotency_key: Mapped[str] = mapped_column(String(64), nullable=False)
    target: Mapped[str] = mapped_column(Text, nullable=False)
    payload: Mapped[dict[str, JsonValue]] = mapped_column(json_type, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False)
    claim_token: Mapped[str | None] = mapped_column(String(64), nullable=True)
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
