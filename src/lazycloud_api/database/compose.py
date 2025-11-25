import uuid
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, List

from cryptography.fernet import InvalidToken
from pydantic import field_serializer, field_validator
from sqlalchemy import (
    UUID,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    func,
    or_,
    select,
    text,
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
from lazycloud_api.database.workspaces import WorkspaceTable
from shared.models.deployments import DeploymentStates
from shared.models.helm import HelmValues

if TYPE_CHECKING:
    from lazycloud_api.database.secrets import SecretTable


class ComposeDeploymentTable(BaseTable):
    """SQLAlchemy model for a compose deployment."""

    __tablename__ = "compose_deployments"

    name: Mapped[str | None] = mapped_column(String, index=True)
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


class ComposeDeploymentPydantic(BaseDbPydanticModel):
    """Pydantic model for a compose deployment with automatic encryption/decryption."""

    name: str | None = None
    workspace_id: UUIDStr
    namespace: str
    compose_yaml: str
    pending_compose_yaml: str | None = None
    helm_values: HelmValues | None = None
    current_helm_revision: int | None = None
    state: DeploymentStates = DeploymentStates.PENDING
    status_message: str | None = None
    deployed_at: datetime | None = None
    deleted_at: datetime | None = None
    current_task_run_id: UUIDStr | None = None
    depot_project_id: str | None = None

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

    @field_serializer("current_task_run_id", when_used="always")
    def serialize_task_run_id(self, value: uuid.UUID | str | None) -> str | None:
        """Ensure task_run_id is serialized as string for Prefect compatibility."""
        if value is None:
            return None
        if isinstance(value, uuid.UUID):
            return str(value)
        return value

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


class ComposeDeploymentService(
    DatabaseService[ComposeDeploymentTable, ComposeDeploymentPydantic]
):
    """Service layer for compose deployment operations"""

    def __init__(self):
        super().__init__(ComposeDeploymentTable, ComposeDeploymentPydantic)

    async def get_by_name(
        self, workspace_id: str, name: str
    ) -> ComposeDeploymentPydantic | None:
        """Get deployment by name (excluding soft-deleted)."""
        async with self._session_manager.get_session() as session:
            query = (
                select(ComposeDeploymentTable)
                .where(ComposeDeploymentTable.workspace_id == workspace_id)
                .where(ComposeDeploymentTable.name == name)
                .where(ComposeDeploymentTable.deleted_at.is_(None))
            )
            result = await session.execute(query)
            db_model = result.scalar_one_or_none()
            return self._to_pydantic(db_model)

    async def find_by_namespace(
        self, workspace_id: str, namespace: str
    ) -> ComposeDeploymentPydantic | None:
        """Find deployment by namespace and workspace (excluding soft-deleted)."""
        async with self._session_manager.get_session() as session:
            query = (
                select(ComposeDeploymentTable)
                .where(ComposeDeploymentTable.workspace_id == workspace_id)
                .where(ComposeDeploymentTable.namespace == namespace)
                .where(ComposeDeploymentTable.deleted_at.is_(None))
            )
            result = await session.execute(query)
            db_model = result.scalar_one_or_none()
            return self._to_pydantic(db_model)

    async def find_by_status(
        self, workspace_id: str, state: DeploymentStates
    ) -> list[ComposeDeploymentPydantic]:
        """Find deployments by status (excluding soft-deleted)."""
        async with self._session_manager.get_session() as session:
            query = (
                select(ComposeDeploymentTable)
                .where(ComposeDeploymentTable.workspace_id == workspace_id)
                .where(ComposeDeploymentTable.state == state)
                .where(ComposeDeploymentTable.deleted_at.is_(None))
            )
            result = await session.execute(query)
            db_models = list(result.scalars().all())
            return [self._to_pydantic(db_model) for db_model in db_models]

    async def update_status(
        self,
        deployment_id: str,
        state: DeploymentStates,
        message: str | None = None,
    ) -> ComposeDeploymentPydantic | None:
        """Update deployment status."""
        deployment = await self.get_by_id(deployment_id)
        if not deployment:
            return None

        deployment.state = state
        if message:
            deployment.status_message = message

        return await self.update(deployment)

    async def get_active_deployments_for_workspace(
        self, workspace_id: str
    ) -> dict[str, str]:
        """Get mapping of deployment_name -> deployment_id for active deployments."""
        async with self._session_manager.get_session() as session:
            query = (
                select(ComposeDeploymentTable.name, ComposeDeploymentTable.id)
                .where(ComposeDeploymentTable.workspace_id == workspace_id)
                .where(ComposeDeploymentTable.deleted_at.is_(None))
            )
            result = await session.execute(query)
            return {
                row.name: str(row.id) for row in result.all() if row.name is not None
            }

    async def find_one_with_lock(
        self,
        workspace_id: str,
        name: str,
        session: AsyncSession,
    ) -> ComposeDeploymentPydantic | None:
        """Find a deployment by workspace and name with row-level lock (excluding soft-deleted)"""
        query = (
            select(ComposeDeploymentTable)
            .where(ComposeDeploymentTable.workspace_id == workspace_id)
            .where(ComposeDeploymentTable.name == name)
            .where(ComposeDeploymentTable.deleted_at.is_(None))
            .with_for_update()
        )
        result = await session.execute(query)
        db_model = result.scalar_one_or_none()
        return self._to_pydantic(db_model)

    async def get_with_workspace_access(
        self, deployment_id: str, user_id: str, include_deleted: bool = False
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

            if not include_deleted:
                query = query.where(ComposeDeploymentTable.deleted_at.is_(None))

            result = await session.execute(query)
            row = result.first()

            if not row:
                return None, None

            deployment, role = row
            deployment_pydantic = self._to_pydantic(deployment)
            return deployment_pydantic, role

    async def find_active_during_date_range(
        self,
        workspace_id: str,
        start_date: datetime,
        end_date: datetime,
        limit: int = 100,
    ) -> list[ComposeDeploymentPydantic]:
        """Get deployments that existed during the date range.

        Includes:
        - Active deployments (deleted_at IS NULL)
        - Deleted deployments that were deleted during or after the date range
        - Only deployments created before or during the date range
        """
        async with self._session_manager.get_session() as session:
            query = (
                select(ComposeDeploymentTable)
                .where(ComposeDeploymentTable.workspace_id == workspace_id)
                .where(
                    or_(
                        ComposeDeploymentTable.deleted_at.is_(None),
                        ComposeDeploymentTable.deleted_at >= start_date,
                    )
                )
                .where(ComposeDeploymentTable.created_at <= end_date)
                .limit(limit)
            )
            result = await session.execute(query)
            return [self._to_pydantic(d) for d in result.scalars().all()]

    async def get_deployment_count(self, workspace_id: str) -> int:
        """Count active deployments for a workspace using a single COUNT query."""
        async with self._session_manager.get_session() as session:
            query = (
                select(func.count(ComposeDeploymentTable.id))
                .where(ComposeDeploymentTable.workspace_id == workspace_id)
                .where(ComposeDeploymentTable.deleted_at.is_(None))
            )
            result = await session.execute(query)
            return result.scalar() or 0

    async def get_deployment_counts_by_workspace(
        self, workspace_ids: list[str]
    ) -> dict[str, int]:
        """Get deployment counts for multiple workspaces in a single query."""
        if not workspace_ids:
            return {}

        async with self._session_manager.get_session() as session:
            query = (
                select(
                    ComposeDeploymentTable.workspace_id,
                    func.count(ComposeDeploymentTable.id).label("deployment_count"),
                )
                .where(ComposeDeploymentTable.workspace_id.in_(workspace_ids))
                .where(ComposeDeploymentTable.deleted_at.is_(None))
                .group_by(ComposeDeploymentTable.workspace_id)
            )
            result = await session.execute(query)
            return {str(row.workspace_id): row.deployment_count for row in result.all()}

    async def find_stuck_deploying(
        self, minutes_old: int = 10
    ) -> list[ComposeDeploymentPydantic]:
        """Find deployments in DEPLOYING state older than specified minutes."""
        async with self._session_manager.get_session() as session:
            threshold = datetime.now(UTC) - timedelta(minutes=minutes_old)
            query = (
                select(ComposeDeploymentTable)
                .where(ComposeDeploymentTable.state == DeploymentStates.DEPLOYING)
                .where(ComposeDeploymentTable.updated_at < threshold)
                .where(ComposeDeploymentTable.deleted_at.is_(None))
            )
            result = await session.execute(query)
            db_models = list(result.scalars().all())
            return [self._to_pydantic(db_model) for db_model in db_models]

    async def find_orphaned_in_deleted_workspaces(
        self,
    ) -> list[ComposeDeploymentPydantic]:
        """Find deployments in deleted workspaces that haven't been cleaned up yet.

        Returns deployments where:
        - workspace.deleted_at IS NOT NULL (workspace is deleted)
        - deployment.deleted_at IS NOT NULL (deployment marked for deletion)
        - deployment.state != DELETED (not yet cleaned up)
        - deployment.current_task_run_id IS NULL (no active destroy task running)
        """
        async with self._session_manager.get_session() as session:
            query = (
                select(ComposeDeploymentTable)
                .join(
                    WorkspaceTable,
                    ComposeDeploymentTable.workspace_id == WorkspaceTable.id,
                )
                .where(WorkspaceTable.deleted_at.isnot(None))
                .where(ComposeDeploymentTable.deleted_at.isnot(None))
                .where(ComposeDeploymentTable.state != DeploymentStates.DELETED.value)
                .where(ComposeDeploymentTable.current_task_run_id.is_(None))
            )
            result = await session.execute(query)
            db_models = list(result.scalars().all())
            return [self._to_pydantic(db_model) for db_model in db_models]
