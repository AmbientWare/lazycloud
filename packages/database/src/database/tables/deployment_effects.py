from datetime import datetime

from pydantic import JsonValue
from sqlalchemy import BigInteger, Boolean, CheckConstraint, DateTime, ForeignKey, Index, String
from sqlalchemy.orm import Mapped, mapped_column

from database.tables.base import DatabaseBase, IdTable, json_type, uuid_type


class DeploymentPreparationTable(DatabaseBase):
    __tablename__ = "deployment_preparations"
    __table_args__ = (Index("ix_deployment_preparations_expiry", "expires_at"),)

    id: Mapped[str] = mapped_column(uuid_type, primary_key=True)
    workspace_id: Mapped[str] = mapped_column(
        uuid_type, ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False
    )
    deployment: Mapped[dict[str, JsonValue]] = mapped_column(json_type, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class DeploymentEffectTable(IdTable, DatabaseBase):
    __tablename__ = "deployment_effects"
    __table_args__ = (
        Index("ix_deployment_effects_due", "retry_at", "id"),
        CheckConstraint(
            "action IN ('created', 'started', 'stopped', 'deleted', 'scale.updated')",
            name="ck_deployment_effects_action",
        ),
    )

    workspace_id: Mapped[str] = mapped_column(
        uuid_type, ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False
    )
    deployment_id: Mapped[str] = mapped_column(
        uuid_type, ForeignKey("deployments.id", ondelete="CASCADE"), nullable=False
    )
    action: Mapped[str] = mapped_column(String(32), nullable=False)
    retry_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    app_created: Mapped[bool] = mapped_column(Boolean, nullable=False)
    source_stub_id: Mapped[str | None] = mapped_column(uuid_type)
    source_deleted: Mapped[bool] = mapped_column(Boolean, nullable=False)
    app_revision: Mapped[int | None] = mapped_column(BigInteger)


class DeploymentShutdownTable(DatabaseBase):
    __tablename__ = "deployment_shutdowns"

    effect_id: Mapped[str] = mapped_column(
        uuid_type, ForeignKey("deployment_effects.id", ondelete="CASCADE"), primary_key=True
    )
    container_id: Mapped[str] = mapped_column(
        uuid_type, ForeignKey("containers.id", ondelete="CASCADE"), primary_key=True
    )
    worker_id: Mapped[str] = mapped_column(String(240), nullable=False)
