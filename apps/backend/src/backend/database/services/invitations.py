from datetime import datetime, timezone

from sqlalchemy import delete, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select

from backend.database.models import (
    InvitationType,
    WorkspaceInvitation,
    WorkspaceInvitationInDb,
    WorkspaceRole,
)
from backend.database.services.base import DatabaseService
from backend.database.tables import WorkspaceInvitationTable


class WorkspaceInvitationService(
    DatabaseService[WorkspaceInvitationTable, WorkspaceInvitationInDb]
):
    """Service layer for workspace invitation operations."""

    def __init__(self, session: AsyncSession):
        super().__init__(WorkspaceInvitationTable, WorkspaceInvitationInDb, session)

    async def create_invitation(
        self,
        workspace_id: str,
        email: str,
        role: WorkspaceRole,
        invited_by_user_id: str,
        token: str,
        expires_at: datetime,
        invitation_type: str = InvitationType.MEMBER.value,
    ) -> WorkspaceInvitationInDb:
        """Create a new invitation."""
        invitation = WorkspaceInvitation(
            workspace_id=workspace_id,
            email=email.lower().strip(),
            role=role,
            token=token,
            invited_by_user_id=invited_by_user_id,
            expires_at=expires_at,
            invitation_type=invitation_type,
        )
        return await self.create(invitation)

    async def get_by_token(self, token: str) -> WorkspaceInvitationInDb | None:
        """Get invitation by token."""
        return await self.find_one(filters={"token": token})

    async def get_by_workspace(
        self, workspace_id: str, include_accepted: bool = False
    ) -> list[WorkspaceInvitationInDb]:
        """Get all invitations for a workspace."""
        query = select(WorkspaceInvitationTable).where(
            WorkspaceInvitationTable.workspace_id == workspace_id
        )
        if not include_accepted:
            query = query.where(WorkspaceInvitationTable.accepted_at.is_(None))
        result = await self._session.execute(query)
        return [self._to_pydantic(inv) for inv in result.scalars().all()]

    async def get_pending_by_email(self, email: str) -> list[WorkspaceInvitationInDb]:
        """Get all pending invitations for an email."""
        query = (
            select(WorkspaceInvitationTable)
            .where(WorkspaceInvitationTable.email == email.lower().strip())
            .where(WorkspaceInvitationTable.accepted_at.is_(None))
            .where(WorkspaceInvitationTable.expires_at > datetime.now(timezone.utc))
        )
        result = await self._session.execute(query)
        return [self._to_pydantic(inv) for inv in result.scalars().all()]

    async def accept_invitation(
        self, invitation_id: str
    ) -> WorkspaceInvitationInDb | None:
        """Mark invitation as accepted."""
        stmt = (
            update(WorkspaceInvitationTable)
            .where(WorkspaceInvitationTable.id == invitation_id)
            .where(WorkspaceInvitationTable.accepted_at.is_(None))
            .values(
                accepted_at=datetime.now(timezone.utc),
                updated_at=datetime.now(timezone.utc),
            )
            .returning(WorkspaceInvitationTable)
        )
        result = await self._session.execute(
            stmt, execution_options={"populate_existing": True}
        )
        db_model = result.scalar_one_or_none()
        return self._to_pydantic(db_model) if db_model else None

    async def delete_expired(self) -> int:
        """Delete expired invitations that haven't been accepted."""
        stmt = (
            delete(WorkspaceInvitationTable)
            .where(WorkspaceInvitationTable.expires_at < datetime.now(timezone.utc))
            .where(WorkspaceInvitationTable.accepted_at.is_(None))
            .returning(WorkspaceInvitationTable.id)
        )
        result = await self._session.execute(stmt)
        return len(result.scalars().all())

    async def get_by_workspace_and_email(
        self, workspace_id: str, email: str
    ) -> WorkspaceInvitationInDb | None:
        """Get invitation by workspace and email."""
        return await self.find_one(
            filters={
                "workspace_id": workspace_id,
                "email": email.lower().strip(),
            }
        )

    async def get_by_workspace_and_email_and_type(
        self, workspace_id: str, email: str, invitation_type: str
    ) -> WorkspaceInvitationInDb | None:
        """Get invitation by workspace, email, and invitation type."""
        return await self.find_one(
            filters={
                "workspace_id": workspace_id,
                "email": email.lower().strip(),
                "invitation_type": invitation_type,
            }
        )
