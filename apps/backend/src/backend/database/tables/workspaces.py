from datetime import datetime
from typing import TYPE_CHECKING, List

from sqlalchemy import Boolean, DateTime, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from backend.database.models import WorkspaceStatus
from backend.database.tables.base import BaseTable
from backend.database.tables.user_workspaces import UserWorkspaceTable

if TYPE_CHECKING:
    from backend.database.tables.compose import ComposeDeploymentTable


class WorkspaceTable(BaseTable):
    """SQLAlchemy model for a workspace"""

    __tablename__ = "workspaces"

    name: Mapped[str] = mapped_column(String)
    is_personal: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    status: Mapped[str] = mapped_column(String, default=WorkspaceStatus.ACTIVE.value)
    deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )

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
