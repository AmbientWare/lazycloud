import uuid
from enum import StrEnum
from typing import TYPE_CHECKING

from sqlalchemy import UUID, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from lazycloud_api.database.base import (
    BaseDbPydanticModel,
    BaseTable,
    DatabaseService,
    UUIDStr,
)

if TYPE_CHECKING:
    from lazycloud_api.database.users import UserTable
    from lazycloud_api.database.workspaces import WorkspaceTable


class WorkspaceRole(StrEnum):
    """Role of a user in a workspace"""

    OWNER = "owner"
    ADMIN = "admin"
    MEMBER = "member"


class UserWorkspaceStatus(StrEnum):
    """Status of user membership in a workspace"""

    ACTIVE = "active"
    INVITED = "invited"
    SUSPENDED = "suspended"


class UserWorkspaceTable(BaseTable):
    """Association object for many-to-many relationship between users and workspaces"""

    __tablename__ = "user_workspaces"

    # Foreign keys
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        primary_key=True,
    )
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        primary_key=True,
    )

    # Additional relationship metadata
    role: Mapped[str] = mapped_column(String, default=WorkspaceRole.MEMBER, index=True)
    status: Mapped[str] = mapped_column(
        String, default=UserWorkspaceStatus.ACTIVE, index=True
    )

    # Relationships to actual objects
    user: Mapped["UserTable"] = relationship(
        "UserTable",
        back_populates="user_workspaces",
        lazy="selectin",
    )
    workspace: Mapped["WorkspaceTable"] = relationship(
        "WorkspaceTable",
        back_populates="user_workspaces",
        lazy="selectin",
    )


class UserWorkspacePydantic(BaseDbPydanticModel):
    """Pydantic model for user-workspace membership"""

    user_id: UUIDStr
    workspace_id: UUIDStr
    role: WorkspaceRole
    status: UserWorkspaceStatus


class UserWorkspaceService(DatabaseService[UserWorkspaceTable, UserWorkspacePydantic]):
    """Service layer for user-workspace membership operations"""

    def __init__(self):
        super().__init__(UserWorkspaceTable, UserWorkspacePydantic)

    async def aget_by_user_and_workspace(
        self, user_id: str, workspace_id: str
    ) -> UserWorkspacePydantic | None:
        """Get membership by user and workspace"""
        filters = {"user_id": user_id, "workspace_id": workspace_id}
        return await self.afind_one(filters=filters)

    async def aget_user_memberships(self, user_id: str) -> list[UserWorkspacePydantic]:
        """Get all workspace memberships for a user"""
        return await self.afind({"user_id": user_id})

    async def aget_workspace_members(
        self, workspace_id: str
    ) -> list[UserWorkspacePydantic]:
        """Get all members of a workspace"""
        return await self.afind({"workspace_id": workspace_id})

    async def aupdate_role(
        self, user_id: str, workspace_id: str, role: WorkspaceRole
    ) -> UserWorkspacePydantic | None:
        """Update a user's role in a workspace"""
        membership = await self.aget_by_user_and_workspace(user_id, workspace_id)
        if not membership:
            return None

        membership.role = role
        return await self.aupdate(membership)

    async def aupdate_status(
        self, user_id: str, workspace_id: str, status: UserWorkspaceStatus
    ) -> UserWorkspacePydantic | None:
        """Update a user's status in a workspace"""
        membership = await self.aget_by_user_and_workspace(user_id, workspace_id)
        if not membership:
            return None

        membership.status = status
        return await self.aupdate(membership)
