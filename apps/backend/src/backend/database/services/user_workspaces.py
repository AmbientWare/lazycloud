from datetime import datetime, timezone

from sqlalchemy import and_, func, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select

from backend.database.models import (
    InvitationType,
    UserInDb,
    UserWorkspaceInDb,
    UserWorkspaceStatus,
    WorkspaceRole,
)
from backend.database.services.base import DatabaseService
from backend.database.tables import (
    UserTable,
    UserWorkspaceTable,
    WorkspaceInvitationTable,
)


class UserWorkspaceService(DatabaseService[UserWorkspaceTable, UserWorkspaceInDb]):
    """Service layer for user-workspace membership operations."""

    def __init__(self, session: AsyncSession):
        super().__init__(UserWorkspaceTable, UserWorkspaceInDb, session)

    async def get_by_user_and_workspace(
        self,
        user_id: str,
        workspace_id: str,
    ) -> UserWorkspaceInDb | None:
        """Get membership by user and workspace."""
        return await self.find_one(
            filters={"user_id": user_id, "workspace_id": workspace_id}
        )

    async def get_user_memberships(self, user_id: str) -> list[UserWorkspaceInDb]:
        """Get all workspace memberships for a user."""
        return await self.find({"user_id": user_id})

    async def get_workspace_members(self, workspace_id: str) -> list[UserWorkspaceInDb]:
        """Get all members of a workspace."""
        return await self.find({"workspace_id": workspace_id})

    async def get_workspace_members_with_users(
        self, workspace_id: str
    ) -> list[tuple[UserWorkspaceInDb, UserInDb]]:
        """Get all members of a workspace with their user information."""
        query = (
            select(UserWorkspaceTable, UserTable)
            .join(UserTable, UserWorkspaceTable.user_id == UserTable.id)
            .where(UserWorkspaceTable.workspace_id == workspace_id)
        )
        result = await self._session.execute(query)
        return [
            (self._to_pydantic(member), user.to_pydantic(UserInDb))
            for member, user in result.all()
        ]

    async def get_workspace_members_with_invitations(
        self, workspace_id: str
    ) -> list[tuple[UserWorkspaceInDb, UserInDb, str | None]]:
        """Get all members of a workspace with their user information and pending invitation IDs."""
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
        return [
            (
                self._to_pydantic(member),
                user.to_pydantic(UserInDb),
                str(invitation_id) if invitation_id is not None else None,
            )
            for member, user, invitation_id in result.all()
        ]

    async def update_role(
        self,
        user_id: str,
        workspace_id: str,
        role: WorkspaceRole,
    ) -> UserWorkspaceInDb | None:
        """Update a user's role in a workspace."""
        stmt = (
            update(UserWorkspaceTable)
            .where(UserWorkspaceTable.user_id == user_id)
            .where(UserWorkspaceTable.workspace_id == workspace_id)
            .values(role=role, updated_at=datetime.now(timezone.utc))
            .returning(UserWorkspaceTable)
        )
        result = await self._session.execute(
            stmt, execution_options={"populate_existing": True}
        )
        db_model = result.scalar_one_or_none()
        return self._to_pydantic(db_model) if db_model else None

    async def update_status(
        self, user_id: str, workspace_id: str, status: UserWorkspaceStatus
    ) -> UserWorkspaceInDb | None:
        """Update a user's status in a workspace."""
        stmt = (
            update(UserWorkspaceTable)
            .where(UserWorkspaceTable.user_id == user_id)
            .where(UserWorkspaceTable.workspace_id == workspace_id)
            .values(status=status, updated_at=datetime.now(timezone.utc))
            .returning(UserWorkspaceTable)
        )
        result = await self._session.execute(
            stmt, execution_options={"populate_existing": True}
        )
        db_model = result.scalar_one_or_none()
        return self._to_pydantic(db_model) if db_model else None
