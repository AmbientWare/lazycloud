import uuid
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from models.workspaces import InvitationType, WorkspaceRole
from sqlalchemy import DateTime, ForeignKey, Index, String, and_, delete
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from sqlalchemy.orm import Mapped, mapped_column, relationship

from backend.database.base import (
    BaseDbPydanticModel,
    BaseTable,
    DatabaseService,
    UUIDStr,
)
from backend.database.users import UserTable

if TYPE_CHECKING:
    from backend.database.workspaces import WorkspaceTable


class WorkspaceInvitationTable(BaseTable):
    """Table for workspace invitations"""

    __tablename__ = "workspace_invitations"

    workspace_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        index=True,
    )
    email: Mapped[str] = mapped_column(String, index=True)
    role: Mapped[str] = mapped_column(String, default=WorkspaceRole.MEMBER)
    token: Mapped[str] = mapped_column(String, unique=True, index=True)
    invitation_type: Mapped[str] = mapped_column(
        String, default=InvitationType.MEMBER.value
    )
    invited_by_user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE")
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    accepted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    workspace: Mapped["WorkspaceTable"] = relationship(
        "WorkspaceTable",
        lazy="selectin",
    )
    invited_by: Mapped["UserTable"] = relationship(
        "UserTable",
        lazy="selectin",
    )

    __table_args__ = (
        Index("idx_workspace_invitations_workspace_email", "workspace_id", "email"),
    )


class WorkspaceInvitationPydantic(BaseDbPydanticModel):
    """Pydantic model for workspace invitation"""

    workspace_id: UUIDStr
    email: str
    role: WorkspaceRole
    token: str
    invited_by_user_id: UUIDStr
    expires_at: datetime
    accepted_at: datetime | None = None
    invitation_type: str = InvitationType.MEMBER.value


class WorkspaceInvitationService(
    DatabaseService[WorkspaceInvitationTable, WorkspaceInvitationPydantic]
):
    """Service layer for workspace invitation operations"""

    def __init__(self, session: AsyncSession):
        super().__init__(WorkspaceInvitationTable, WorkspaceInvitationPydantic, session)

    async def create_invitation(
        self,
        workspace_id: str,
        email: str,
        role: WorkspaceRole,
        invited_by_user_id: str,
        token: str,
        expires_at: datetime,
        invitation_type: str = InvitationType.MEMBER.value,
    ) -> WorkspaceInvitationPydantic:
        """Create a new invitation"""
        invitation = WorkspaceInvitationPydantic(
            workspace_id=workspace_id,
            email=email.lower().strip(),
            role=role,
            token=token,
            invited_by_user_id=invited_by_user_id,
            expires_at=expires_at,
            invitation_type=invitation_type,
        )
        return await self.create(invitation)

    async def get_by_token(self, token: str) -> WorkspaceInvitationPydantic | None:
        """Get invitation by token"""
        filters = {"token": token}
        return await self.find_one(filters=filters)

    async def get_by_workspace(
        self, workspace_id: str, include_accepted: bool = False
    ) -> list[WorkspaceInvitationPydantic]:
        """Get all invitations for a workspace"""
        query = select(WorkspaceInvitationTable).where(
            WorkspaceInvitationTable.workspace_id == workspace_id
        )
        if not include_accepted:
            query = query.where(WorkspaceInvitationTable.accepted_at.is_(None))
        result = await self._session.execute(query)
        invitations = result.scalars().all()
        return [self._to_pydantic(inv) for inv in invitations if inv]

    async def get_pending_by_email(
        self, email: str
    ) -> list[WorkspaceInvitationPydantic]:
        """Get all pending invitations for an email"""
        query = (
            select(WorkspaceInvitationTable)
            .where(WorkspaceInvitationTable.email == email.lower().strip())
            .where(WorkspaceInvitationTable.accepted_at.is_(None))
            .where(WorkspaceInvitationTable.expires_at > datetime.now(timezone.utc))
        )
        result = await self._session.execute(query)
        invitations = result.scalars().all()
        return [self._to_pydantic(inv) for inv in invitations if inv]

    async def accept_invitation(
        self, invitation_id: str
    ) -> WorkspaceInvitationPydantic | None:
        """Mark invitation as accepted"""
        invitation = await self.get_by_id(invitation_id)
        if not invitation:
            return None

        invitation.accepted_at = datetime.now(timezone.utc)
        return await self.update(invitation)

    async def delete_expired(self) -> int:
        """Delete expired invitations that haven't been accepted"""
        query = delete(WorkspaceInvitationTable).where(
            and_(
                WorkspaceInvitationTable.expires_at < datetime.now(timezone.utc),
                WorkspaceInvitationTable.accepted_at.is_(None),
            )
        )
        result = await self._session.execute(query)
        return result.rowcount or 0

    async def get_by_workspace_and_email(
        self, workspace_id: str, email: str
    ) -> WorkspaceInvitationPydantic | None:
        """Get invitation by workspace and email"""
        filters = {
            "workspace_id": workspace_id,
            "email": email.lower().strip(),
        }
        return await self.find_one(filters=filters)

    async def get_by_workspace_and_email_and_type(
        self, workspace_id: str, email: str, invitation_type: str
    ) -> WorkspaceInvitationPydantic | None:
        """Get invitation by workspace, email, and invitation type"""
        filters = {
            "workspace_id": workspace_id,
            "email": email.lower().strip(),
            "invitation_type": invitation_type,
        }
        return await self.find_one(filters=filters)
