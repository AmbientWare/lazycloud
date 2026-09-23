from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, String, text
from sqlalchemy.orm import Mapped, mapped_column

from database.tables.base import DatabaseBase, IdTable, uuid_type


class DeploymentPruneTable(IdTable, DatabaseBase):
    __tablename__ = "deployment_prunes"
    __table_args__ = (
        Index(
            "ix_deployment_prunes_pending",
            "retry_at",
            postgresql_where=text("completed_at IS NULL"),
        ),
    )

    workspace_id: Mapped[str] = mapped_column(
        uuid_type, ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False
    )
    app_id: Mapped[str] = mapped_column(
        uuid_type, ForeignKey("apps.id", ondelete="CASCADE"), nullable=False
    )
    request_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    retry_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class DeploymentPruneTargetTable(DatabaseBase):
    __tablename__ = "deployment_prune_targets"

    operation_id: Mapped[str] = mapped_column(
        uuid_type, ForeignKey("deployment_prunes.id", ondelete="CASCADE"), primary_key=True
    )
    deployment_id: Mapped[str] = mapped_column(
        uuid_type, ForeignKey("deployments.id", ondelete="CASCADE"), primary_key=True
    )
