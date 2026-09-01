from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql.schema import SchemaItem

from database.tables.base import DatabaseBase, TimestampMixin, uuid_type


class EndpointDispatchTable(TimestampMixin, DatabaseBase):
    __tablename__ = "endpoint_dispatches"
    __table_args__: tuple[SchemaItem, ...] = (
        CheckConstraint(
            "status IN ('queued', 'waiting-capacity', 'inflight', 'complete', "
            "'failed', 'timeout', 'cancelled')",
            name="ck_endpoint_dispatches_status",
        ),
        CheckConstraint(
            "wait_timeout_seconds > 0",
            name="ck_endpoint_dispatches_wait_timeout",
        ),
        CheckConstraint(
            "max_pending_requests > 0",
            name="ck_endpoint_dispatches_max_pending",
        ),
        CheckConstraint(
            "max_inflight_per_container > 0",
            name="ck_endpoint_dispatches_max_inflight",
        ),
        CheckConstraint("attempts >= 0", name="ck_endpoint_dispatches_attempts"),
        Index(
            "ix_endpoint_dispatches_stub_active_expiry",
            "stub_id",
            "expires_at",
            postgresql_where=text("status IN ('queued', 'waiting-capacity', 'inflight')"),
            sqlite_where=text("status IN ('queued', 'waiting-capacity', 'inflight')"),
        ),
        Index(
            "ix_endpoint_dispatches_stub_container_inflight",
            "stub_id",
            "container_id",
            "expires_at",
            postgresql_where=text("status = 'inflight'"),
            sqlite_where=text("status = 'inflight'"),
        ),
        Index(
            "ix_endpoint_dispatches_stub_container_finished",
            "stub_id",
            "container_id",
            "finished_at",
            postgresql_where=text("container_id IS NOT NULL AND finished_at IS NOT NULL"),
            sqlite_where=text("container_id IS NOT NULL AND finished_at IS NOT NULL"),
        ),
    )

    task_id: Mapped[str] = mapped_column(
        uuid_type,
        ForeignKey("tasks.id", ondelete="CASCADE"),
        primary_key=True,
    )
    workspace_id: Mapped[str] = mapped_column(
        uuid_type,
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=False,
    )
    stub_id: Mapped[str] = mapped_column(
        uuid_type,
        ForeignKey("stubs.id", ondelete="CASCADE"),
        nullable=False,
    )
    container_id: Mapped[str | None] = mapped_column(
        uuid_type,
        ForeignKey("containers.id", ondelete="SET NULL"),
        nullable=True,
    )
    method: Mapped[str] = mapped_column(String(32), nullable=False)
    path: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    wait_timeout_seconds: Mapped[float] = mapped_column(Float, nullable=False)
    max_pending_requests: Mapped[int] = mapped_column(Integer, nullable=False)
    max_inflight_per_container: Mapped[int] = mapped_column(Integer, nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    enqueued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)


__all__ = ["EndpointDispatchTable"]
