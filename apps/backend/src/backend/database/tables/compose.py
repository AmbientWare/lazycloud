import uuid
from datetime import datetime
from typing import TYPE_CHECKING, List

from models.deployments import DeploymentStates
from sqlalchemy import (
    UUID,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    text,
)
from sqlalchemy import (
    Enum as SQLAEnum,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from backend.database.tables.base import (
    BaseTable,
)
from backend.database.tables.workspaces import WorkspaceTable

if TYPE_CHECKING:
    from backend.database.tables.secrets import SecretTable


class ComposeDeploymentTable(BaseTable):
    """SQLAlchemy model for a compose deployment."""

    __tablename__ = "compose_deployments"

    name: Mapped[str] = mapped_column(String, index=True)
    namespace: Mapped[str] = mapped_column(String)
    compose_yaml: Mapped[str] = mapped_column(Text)
    pending_compose_yaml: Mapped[str | None] = mapped_column(Text)
    helm_values: Mapped[str | None] = mapped_column(Text)
    current_helm_revision: Mapped[int | None] = mapped_column(Integer, nullable=True)
    state: Mapped[DeploymentStates] = mapped_column(
        SQLAEnum(DeploymentStates), default=DeploymentStates.PENDING, index=True
    )
    status_message: Mapped[str | None] = mapped_column(Text)
    deployed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    current_task_run_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True, index=True
    )
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("workspaces.id", ondelete="CASCADE")
    )
    depot_project_id: Mapped[str | None] = mapped_column(
        String, nullable=True, index=True
    )
    # cluster_id is required - no default. The API must explicitly set this
    # based on placement logic. Migration backfills existing rows with 'ash-1'.
    cluster_id: Mapped[str] = mapped_column(String(50), nullable=False, index=True)

    # Unique constraint to ensure one deployment per name per workspace (only for non-deleted)
    __table_args__ = (
        Index(
            "uq_workspace_deployment_name",
            "workspace_id",
            "name",
            unique=True,
            postgresql_where=text("deleted_at IS NULL"),
        ),
        Index("ix_compose_deployments_workspace_id_name", "workspace_id", "name"),
    )

    # Relationships
    secrets: Mapped[List["SecretTable"]] = relationship(
        "SecretTable",
        back_populates="deployment",
        cascade="all, delete-orphan",
        passive_deletes=True,
        lazy="selectin",
    )
    workspace: Mapped["WorkspaceTable"] = relationship(
        "WorkspaceTable",
        back_populates="deployments",
        lazy="selectin",
    )
