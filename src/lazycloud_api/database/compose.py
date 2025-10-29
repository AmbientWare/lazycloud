import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Any, List

from cryptography.fernet import InvalidToken
from pydantic import field_serializer, field_validator
from sqlalchemy import (
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
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column, relationship

from lazycloud_api.database.base import (
    BaseDbPydanticModel,
    BaseTable,
    DatabaseService,
    UUIDStr,
)
from lazycloud_api.database.user_workspaces import UserWorkspaceTable
from lazycloud_api.database.utils import (
    decrypt_dict,
    decrypt_string,
    encrypt_dict,
    encrypt_string,
)
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
    pending_compose_yaml: Mapped[str | None] = mapped_column(Text)
    helm_values: Mapped[str | None] = mapped_column(Text)
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
    """Pydantic model for a compose deployment with automatic encryption/decryption."""

    name: str | None = None
    workspace_id: UUIDStr
    namespace: str
    compose_yaml: str
    pending_compose_yaml: str | None = None
    helm_values: HelmValues | None = None
    state: DeploymentStates = DeploymentStates.PENDING
    status_message: str | None = None
    deployed_at: datetime | None = None

    @field_validator("compose_yaml", "pending_compose_yaml", mode="before")
    @classmethod
    def decrypt_compose_fields(cls, value: Any) -> str | None:
        """Automatically decrypt compose fields when loading from database."""
        if value is None:
            return None
        if isinstance(value, str):
            try:
                return decrypt_string(value)
            except InvalidToken:
                # Not encrypted (migration from old data or new deployment)
                return value
            except Exception as e:
                # Real decryption error (corrupt data, wrong key, etc.)
                raise ValueError(f"Failed to decrypt compose field: {e}") from e
        return value

    @field_serializer("compose_yaml", "pending_compose_yaml", when_used="always")
    def serialize_compose_fields(self, value: str | None) -> str | None:
        """Automatically encrypt compose fields when dumping for database storage."""
        if value is None:
            return None
        try:
            return encrypt_string(value)
        except Exception as e:
            raise ValueError(f"Failed to encrypt compose field: {e}") from e

    @field_validator("helm_values", mode="before")
    @classmethod
    def decrypt_helm_values(cls, value: Any) -> HelmValues | None:
        """Automatically decrypt helm_values when loading from database."""
        if value is None:
            return None
        if isinstance(value, str):
            try:
                decrypted_dict = decrypt_dict(value)
                return HelmValues(**decrypted_dict)
            except InvalidToken:
                # Not encrypted - shouldn't happen in normal flow
                raise ValueError(
                    "helm_values is not encrypted - possible data corruption"
                )
            except Exception as e:
                raise ValueError(f"Failed to decrypt helm_values: {e}") from e
        # Already a HelmValues object or dict
        return value

    @field_serializer("helm_values", when_used="always")
    def serialize_helm_values(self, value: HelmValues | None) -> str | None:
        """Automatically encrypt helm_values when dumping for database storage."""
        if value is None:
            return None
        if isinstance(value, HelmValues):
            # Encrypt it
            try:
                helm_dict = value.model_dump(by_alias=True)
                return encrypt_dict(helm_dict)
            except Exception as e:
                raise ValueError(f"Failed to encrypt helm_values: {e}") from e
        # Already encrypted string
        return value


class ComposeDeploymentService(
    DatabaseService[ComposeDeploymentTable, ComposeDeploymentPydantic]
):
    """Service layer for compose deployment operations"""

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

    async def afind_one_with_lock(
        self,
        workspace_id: str,
        name: str,
        session: AsyncSession,
    ) -> ComposeDeploymentPydantic | None:
        """Find a deployment by workspace and name with row-level lock"""
        query = (
            select(ComposeDeploymentTable)
            .where(ComposeDeploymentTable.workspace_id == workspace_id)
            .where(ComposeDeploymentTable.name == name)
            .with_for_update()
        )
        result = await session.execute(query)
        db_model = result.scalar_one_or_none()
        return self._to_pydantic(db_model)

    async def aget_with_workspace_access(
        self, deployment_id: str, user_id: str
    ) -> tuple[ComposeDeploymentPydantic | None, str | None]:
        """Get deployment and user's workspace role"""
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
            deployment_pydantic = self._to_pydantic(deployment)
            return deployment_pydantic, role
