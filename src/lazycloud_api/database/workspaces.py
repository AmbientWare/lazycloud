from typing import TYPE_CHECKING, List

from sqlalchemy import Boolean, String
from sqlalchemy.future import select
from sqlalchemy.orm import Mapped, mapped_column, relationship

from lazycloud_api.database.base import BaseDbPydanticModel, BaseTable, DatabaseService
from lazycloud_api.database.user_workspaces import (
    UserWorkspacePydantic,
    UserWorkspaceService,
    UserWorkspaceTable,
    WorkspaceRole,
)

if TYPE_CHECKING:
    from lazycloud_api.database.compose import ComposeDeploymentTable


class WorkspaceTable(BaseTable):
    """SQLAlchemy model for a workspace"""

    __tablename__ = "workspaces"

    name: Mapped[str] = mapped_column(String)
    is_personal: Mapped[bool] = mapped_column(Boolean, default=False, index=True)

    # Relationships - using Association Object pattern (SQLAlchemy 2.0 best practice)
    user_workspaces: Mapped[List["UserWorkspaceTable"]] = relationship(
        "UserWorkspaceTable",
        back_populates="workspace",
        cascade="all, delete-orphan",
        lazy="selectin",
    )
    deployments: Mapped[List["ComposeDeploymentTable"]] = relationship(
        "ComposeDeploymentTable",
        back_populates="workspace",
        cascade="all, delete-orphan",
        lazy="selectin",
    )


class WorkspacePydantic(BaseDbPydanticModel):
    """Pydantic model for a workspace"""

    name: str
    is_personal: bool = False


class WorkspaceService(DatabaseService[WorkspaceTable, WorkspacePydantic]):
    """Service layer for workspace operations"""

    def __init__(self):
        super().__init__(WorkspaceTable, WorkspacePydantic)

    async def aget_user_workspaces(
        self, user_id: str
    ) -> list[tuple[WorkspacePydantic, UserWorkspacePydantic]]:
        """Get all workspaces for a user with membership info in a single query"""

        async with self._session_manager.get_session() as session:
            query = (
                select(WorkspaceTable, UserWorkspaceTable)
                .join(UserWorkspaceTable)
                .where(UserWorkspaceTable.user_id == user_id)
            )

            result = await session.execute(query)
            rows = result.all()

            return [
                (
                    self._to_pydantic(workspace),
                    UserWorkspacePydantic.model_validate(membership),
                )
                for workspace, membership in rows
            ]

    async def aget_personal_workspace(self, user_id: str) -> WorkspacePydantic | None:
        """Get a user's personal workspace"""
        async with self._session_manager.get_session() as session:
            # Single query with JOIN: workspaces WHERE is_personal AND user is member
            query = (
                select(WorkspaceTable)
                .join(UserWorkspaceTable)
                .where(UserWorkspaceTable.user_id == user_id)
                .where(WorkspaceTable.is_personal.is_(True))
            )

            result = await session.execute(query)
            workspace = result.scalar_one_or_none()
            return self._to_pydantic(workspace)

    async def aget_owner(self, workspace_id: str) -> "UserWorkspacePydantic | None":
        """Get the owner membership for a workspace"""
        service = UserWorkspaceService()
        members = await service.afind(
            {
                "workspace_id": workspace_id,
                "role": WorkspaceRole.OWNER.value,
            }
        )
        return members[0] if members else None

    async def transfer_ownership(
        self, workspace_id: str, current_owner_id: str, new_owner_id: str
    ) -> tuple["UserWorkspacePydantic | None", "UserWorkspacePydantic | None"]:
        """Transfer ownership from one user to another"""
        service = UserWorkspaceService()

        demoted = await service.aupdate_role(
            current_owner_id, workspace_id, WorkspaceRole.ADMIN.value
        )

        promoted = await service.aupdate_role(
            new_owner_id, workspace_id, WorkspaceRole.OWNER.value
        )

        return (demoted, promoted)
