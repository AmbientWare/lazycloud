from datetime import datetime

from sqlalchemy import Boolean, CheckConstraint, DateTime, ForeignKey, Index, String
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql.schema import SchemaItem

from database.tables.base import DatabaseBase, IdTable, uuid_type


class PreviewSessionTable(IdTable, DatabaseBase):
    __tablename__ = "preview_sessions"
    __table_args__: tuple[SchemaItem, ...] = (
        CheckConstraint(
            "status IN ('active', 'stopped', 'expired')", name="ck_preview_session_status"
        ),
        CheckConstraint(
            "(status = 'active') = (ended_at IS NULL)", name="ck_preview_session_ended"
        ),
        Index("uq_preview_session_execution_stub", "execution_stub_id", unique=True),
        Index("uq_preview_session_container", "container_id", unique=True),
        Index("ix_preview_session_status", "status", "id"),
        Index("ix_preview_session_workspace", "workspace_id", "id"),
    )

    workspace_id: Mapped[str] = mapped_column(
        uuid_type, ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False
    )
    source_stub_id: Mapped[str | None] = mapped_column(
        uuid_type, ForeignKey("stubs.id", ondelete="SET NULL")
    )
    execution_stub_id: Mapped[str | None] = mapped_column(
        uuid_type, ForeignKey("stubs.id", ondelete="SET NULL")
    )
    container_id: Mapped[str | None] = mapped_column(
        uuid_type, ForeignKey("containers.id", ondelete="SET NULL")
    )
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    public: Mapped[bool] = mapped_column(Boolean, nullable=False)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
