import uuid
from typing import TYPE_CHECKING

from sqlalchemy import UUID, ForeignKey, String, and_, func
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from sqlalchemy.orm import Mapped, mapped_column, relationship

from lazycloud_api.database.base import (
    BaseDbPydanticModel,
    BaseTable,
    DatabaseService,
    UUIDStr,
)
from lazycloud_api.database.invitations import WorkspaceInvitationTable
from lazycloud_api.database.users import UserPydantic, UserTable
from shared.models.workspaces import InvitationType, UserWorkspaceStatus, WorkspaceRole

if TYPE_CHECKING:
    from lazycloud_api.database.workspaces import WorkspaceTable


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

    def __init__(self, session: AsyncSession):
        super().__init__(UserWorkspaceTable, UserWorkspacePydantic, session)

    async def get_by_user_and_workspace(
        self,
        user_id: str,
        workspace_id: str,
    ) -> UserWorkspacePydantic | None:
        """Get membership by user and workspace"""
        filters = {"user_id": user_id, "workspace_id": workspace_id}
        return await self.find_one(filters=filters)

    async def get_user_memberships(self, user_id: str) -> list[UserWorkspacePydantic]:
        """Get all workspace memberships for a user"""
        return await self.find({"user_id": user_id})

    async def get_workspace_members(
        self, workspace_id: str
    ) -> list[UserWorkspacePydantic]:
        """Get all members of a workspace"""
        return await self.find({"workspace_id": workspace_id})

    async def get_workspace_members_with_users(
        self, workspace_id: str
    ) -> list[tuple[UserWorkspacePydantic, UserPydantic]]:
        """Get all members of a workspace with their user information"""
        query = (
            select(UserWorkspaceTable, UserTable)
            .join(UserTable, UserWorkspaceTable.user_id == UserTable.id)
            .where(UserWorkspaceTable.workspace_id == workspace_id)
        )
        result = await self._session.execute(query)
        rows: list[tuple[UserWorkspaceTable, UserTable]] = result.all()

        return [
            (self._to_pydantic(member), user.to_pydantic(UserPydantic))
            for member, user in rows
        ]

    async def get_workspace_members_with_invitations(
        self, workspace_id: str
    ) -> list[tuple[UserWorkspacePydantic, UserPydantic, str | None]]:
        """Get all members of a workspace with their user information and pending invitation IDs"""
        # LEFT JOIN to get invitation_id for members with pending invitations
        # Join on email (lowercased) and workspace_id, and filter for pending (not accepted) invitations
        query = (
            select(
                UserWorkspaceTable,
                UserTable,
                WorkspaceInvitationTable.id.label("invitation_id"),
            )
            .join(UserTable, UserWorkspaceTable.user_id == UserTable.id)
            .outerjoin(
                WorkspaceInvitationTable,
                and_(
                    func.lower(UserTable.email)
                    == func.lower(WorkspaceInvitationTable.email),
                    WorkspaceInvitationTable.workspace_id == workspace_id,
                    WorkspaceInvitationTable.accepted_at.is_(None),
                    WorkspaceInvitationTable.invitation_type
                    != InvitationType.OWNERSHIP_TRANSFER.value,
                ),
            )
            .where(UserWorkspaceTable.workspace_id == workspace_id)
        )
        result = await self._session.execute(query)
        rows = result.all()

        return [
            (
                self._to_pydantic(member),
                user.to_pydantic(UserPydantic),
                str(invitation_id) if invitation_id is not None else None,
            )
            for member, user, invitation_id in rows
        ]

    async def update_role(
        self,
        user_id: str,
        workspace_id: str,
        role: WorkspaceRole,
    ) -> UserWorkspacePydantic | None:
        """Update a user's role in a workspace"""
        membership = await self.get_by_user_and_workspace(user_id, workspace_id)
        if not membership:
            return None

        membership.role = role
        return await self.update(membership)

    async def update_status(
        self, user_id: str, workspace_id: str, status: UserWorkspaceStatus
    ) -> UserWorkspacePydantic | None:
        """Update a user's status in a workspace"""
        membership = await self.get_by_user_and_workspace(user_id, workspace_id)
        if not membership:
            return None

        membership.status = status
        return await self.update(membership)
