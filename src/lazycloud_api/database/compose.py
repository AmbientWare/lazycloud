import uuid
from datetime import datetime
from typing import TYPE_CHECKING, List

from sqlalchemy import (
    JSON,
    UUID,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
    select,
)
from sqlalchemy import (
    Enum as SQLAEnum,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from lazycloud_api.database.base import (
    BaseDbPydanticModel,
    BaseTable,
    DatabaseService,
    UUIDStr,
)
from lazycloud_api.database.user_workspaces import UserWorkspaceTable
from shared.models.deployments import DeploymentStates
from shared.models.helm import HelmValues

if TYPE_CHECKING:
    from lazycloud_api.database.secrets import SecretTable
    from lazycloud_api.database.workspaces import WorkspaceTable


class ComposeDeploymentTable(BaseTable):
    """SQLAlchemy model for a compose deployment."""

    __tablename__ = "compose_deployments"

    name: Mapped[str | None] = mapped_column(String, index=True)
    namespace: Mapped[str] = mapped_column(String)
    compose_yaml: Mapped[str] = mapped_column(Text)
    helm_values: Mapped[dict | None] = mapped_column(JSON)
    state: Mapped[DeploymentStates] = mapped_column(
        SQLAEnum(DeploymentStates), default=DeploymentStates.PENDING, index=True
    )
    status_message: Mapped[str | None] = mapped_column(Text)
    deployed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("workspaces.id", ondelete="CASCADE")
    )

    # Unique constraint to ensure one deployment per name per workspace
    __table_args__ = (
        UniqueConstraint("workspace_id", "name", name="uq_workspace_deployment_name"),
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


class ComposeDeploymentPydantic(BaseDbPydanticModel):
    """Pydantic model for a compose deployment."""

    name: str | None = None
    workspace_id: UUIDStr
    namespace: str
    compose_yaml: str
    helm_values: HelmValues | None = None
    state: DeploymentStates = DeploymentStates.PENDING
    status_message: str | None = None
    deployed_at: datetime | None = None


class ComposeDeploymentService(
    DatabaseService[ComposeDeploymentTable, ComposeDeploymentPydantic]
):
    """Service layer for compose deployment operations."""

    def __init__(self):
        super().__init__(ComposeDeploymentTable, ComposeDeploymentPydantic)

    async def aget_by_name(
        self, workspace_id: str, name: str
    ) -> ComposeDeploymentPydantic | None:
        """Get deployment by name."""
        return await self.afind_one({"workspace_id": workspace_id, "name": name})

    async def find_by_namespace(
        self, workspace_id: str, namespace: str
    ) -> ComposeDeploymentPydantic | None:
        """Find deployment by namespace and workspace."""
        return await self.afind_one(
            {"workspace_id": workspace_id, "namespace": namespace}
        )

    async def find_by_status(
        self, workspace_id: str, state: DeploymentStates
    ) -> list[ComposeDeploymentPydantic]:
        """Find deployments by status."""
        return await self.afind({"workspace_id": workspace_id, "state": state})

    async def update_status(
        self,
        deployment_id: str,
        state: DeploymentStates,
        message: str | None = None,
    ) -> ComposeDeploymentPydantic | None:
        """Update deployment status."""
        deployment = await self.aget_by_id(deployment_id)
        if not deployment:
            return None

        deployment.state = state
        if message:
            deployment.status_message = message

        return await self.aupdate(deployment)

    async def aget_with_workspace_access(
        self, deployment_id: str, user_id: str
    ) -> tuple[ComposeDeploymentPydantic | None, str | None]:
        """Get deployment and user's workspace role in a single JOIN query

        Returns:
            Tuple of (deployment, role) or (None, None) if not found or no access
        """
        async with self._session_manager.get_session() as session:
            query = (
                select(ComposeDeploymentTable, UserWorkspaceTable.role)
                .join(
                    UserWorkspaceTable,
                    ComposeDeploymentTable.workspace_id
                    == UserWorkspaceTable.workspace_id,
                )
                .where(ComposeDeploymentTable.id == deployment_id)
                .where(UserWorkspaceTable.user_id == user_id)
            )

            result = await session.execute(query)
            row = result.first()

            if not row:
                return None, None

            deployment, role = row
            return self._to_pydantic(deployment), role
