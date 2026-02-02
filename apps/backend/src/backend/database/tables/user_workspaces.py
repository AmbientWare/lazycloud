import uuid
from typing import TYPE_CHECKING

from sqlalchemy import UUID, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from backend.database.models import UserWorkspaceStatus, WorkspaceRole
from backend.database.tables.base import BaseTable
from backend.database.tables.users import UserTable

if TYPE_CHECKING:
    from backend.database.tables.workspaces import WorkspaceTable


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
