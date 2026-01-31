import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, ForeignKey, Index, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from backend.database.models import InvitationType, WorkspaceRole
from backend.database.tables.base import (
    BaseTable,
)
from backend.database.tables.users import UserTable

if TYPE_CHECKING:
    from backend.database.tables.workspaces import WorkspaceTable


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
