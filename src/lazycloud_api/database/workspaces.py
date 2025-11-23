from datetime import UTC, datetime
from enum import StrEnum
from typing import TYPE_CHECKING, List

from sqlalchemy import Boolean, DateTime, String, and_, func, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from sqlalchemy.orm import Mapped, mapped_column, relationship

from lazycloud_api.database import db
from lazycloud_api.database.base import BaseDbPydanticModel, BaseTable, DatabaseService
from lazycloud_api.database.user_workspaces import (
    UserWorkspacePydantic,
    UserWorkspaceTable,
    WorkspaceRole,
)
from lazycloud_api.database.users import UserPydantic, UserTable

if TYPE_CHECKING:
    from lazycloud_api.database.compose import ComposeDeploymentTable


class WorkspaceStatus(StrEnum):
    """Status of a workspace"""

    ACTIVE = "active"
    INACTIVE = "inactive"
    DELETED = "deleted"


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


class WorkspacePydantic(BaseDbPydanticModel):
    """Pydantic model for a workspace"""

    name: str
    is_personal: bool = False
    status: WorkspaceStatus = WorkspaceStatus.ACTIVE
    deleted_at: datetime | None = None


class WorkspaceService(DatabaseService[WorkspaceTable, WorkspacePydantic]):
    """Service layer for workspace operations"""

    def __init__(self):
        super().__init__(WorkspaceTable, WorkspacePydantic)

    async def get_user_workspaces_with_membership(
        self, user_id: str, status: WorkspaceStatus = WorkspaceStatus.ACTIVE
    ) -> list[tuple[WorkspacePydantic, UserWorkspacePydantic]]:
        """Get all workspaces for a user with membership info in a single query"""

        async with self._session_manager.get_session() as session:
            query = (
                select(WorkspaceTable, UserWorkspaceTable)
                .join(UserWorkspaceTable)
                .where(UserWorkspaceTable.user_id == user_id)
                .where(WorkspaceTable.status == status.value)
            )

            result = await session.execute(query)
            rows = result.all()

            return [
                (
                    self._to_pydantic(workspace),
                    membership.to_pydantic(UserWorkspacePydantic),
                )
                for workspace, membership in rows
            ]

    async def get_personal_workspace(self, user_id: str) -> WorkspacePydantic | None:
        """Get a user's personal workspace"""
        async with self._session_manager.get_session() as session:
            query = (
                select(WorkspaceTable)
                .join(UserWorkspaceTable)
                .where(UserWorkspaceTable.user_id == user_id)
                .where(WorkspaceTable.is_personal.is_(True))
            )

            result = await session.execute(query)
            workspace = result.scalar_one_or_none()
            return self._to_pydantic(workspace)

    async def get_owner(self, workspace_id: str) -> "UserWorkspacePydantic | None":
        """Get the owner membership for a workspace"""
        async with self._session_manager.get_session() as session:
            query = (
                select(UserWorkspaceTable)
                .where(UserWorkspaceTable.workspace_id == workspace_id)
                .where(UserWorkspaceTable.role == WorkspaceRole.OWNER.value)
            )
            result = await session.execute(query)
            membership = result.scalar_one_or_none()

            if membership:
                return db.user_workspaces._to_pydantic(membership)

            return None

    async def get_owner_user(self, workspace_id: str) -> UserPydantic | None:
        """Get the owner user for a workspace in a single query"""

        async with self._session_manager.get_session() as session:
            query = (
                select(UserTable)
                .join(UserWorkspaceTable, UserTable.id == UserWorkspaceTable.user_id)
                .where(UserWorkspaceTable.workspace_id == workspace_id)
                .where(UserWorkspaceTable.role == WorkspaceRole.OWNER.value)
            )

            result = await session.execute(query)
            user = result.scalar_one_or_none()

            if user:
                return user.to_pydantic(UserPydantic)
            return None

    async def transfer_ownership(
        self,
        workspace_id: str,
        current_owner_id: str,
        new_owner_id: str,
        session: AsyncSession | None = None,
    ) -> tuple["UserWorkspacePydantic | None", "UserWorkspacePydantic | None"]:
        """Transfer ownership from one user to another"""

        async def _transfer(sess: AsyncSession):
            # Fetch both memberships
            query = (
                select(UserWorkspaceTable)
                .where(UserWorkspaceTable.workspace_id == workspace_id)
                .where(UserWorkspaceTable.user_id.in_([current_owner_id, new_owner_id]))
            )
            result = await sess.execute(query)
            memberships = result.scalars().all()

            current_owner_membership = None
            new_owner_membership = None

            # Compare as strings
            for membership in memberships:
                if str(membership.user_id) == current_owner_id:
                    current_owner_membership = membership
                elif str(membership.user_id) == new_owner_id:
                    new_owner_membership = membership

            if not current_owner_membership or not new_owner_membership:
                return (None, None)

            # Update both roles
            current_owner_membership.role = WorkspaceRole.ADMIN.value
            new_owner_membership.role = WorkspaceRole.OWNER.value

            await sess.flush()
            await sess.refresh(current_owner_membership)
            await sess.refresh(new_owner_membership)

            return (
                db.user_workspaces._to_pydantic(current_owner_membership),
                db.user_workspaces._to_pydantic(new_owner_membership),
            )

        if session:
            return await _transfer(session)
        else:
            return await self._execute_in_session(_transfer, None)

    async def update_status(
        self, workspace_id: str, status: WorkspaceStatus
    ) -> WorkspacePydantic | None:
        """Update the status of a workspace"""
        async with self._session_manager.get_session() as session:
            update_values = {"status": status.value}
            if status == WorkspaceStatus.DELETED:
                update_values["deleted_at"] = datetime.now(UTC)

            stmt = (
                update(WorkspaceTable)
                .where(WorkspaceTable.id == workspace_id)
                .values(**update_values)
                .returning(WorkspaceTable)
            )
            result = await session.execute(stmt)
            await session.commit()

            updated_workspace = result.scalar_one_or_none()
            return self._to_pydantic(updated_workspace)

    async def get_active_workspaces(self) -> list[WorkspacePydantic]:
        """Get all active workspaces"""
        return await self.find({"status": WorkspaceStatus.ACTIVE.value})

    async def get_active_workspaces_before(
        self, before_date: datetime
    ) -> list[WorkspacePydantic]:
        """Get all active workspaces created before a specific date"""
        async with self._session_manager.get_session() as session:
            query = (
                select(WorkspaceTable)
                .where(WorkspaceTable.status == WorkspaceStatus.ACTIVE.value)
                .where(WorkspaceTable.created_at < before_date)
            )
            result = await session.execute(query)
            workspaces = result.scalars().all()
            return [self._to_pydantic(ws) for ws in workspaces]

    async def get_deleted_in_range(
        self, start_date: datetime, end_date: datetime
    ) -> list[WorkspacePydantic]:
        """Get all workspaces deleted within a date range (inclusive)"""
        async with self._session_manager.get_session() as session:
            query = (
                select(WorkspaceTable)
                .where(WorkspaceTable.status == WorkspaceStatus.DELETED.value)
                .where(
                    and_(
                        WorkspaceTable.deleted_at >= start_date,
                        WorkspaceTable.deleted_at <= end_date,
                    )
                )
            )
            result = await session.execute(query)
            workspaces = result.scalars().all()
            return [self._to_pydantic(ws) for ws in workspaces]

    async def get_user_workspaces_active_during_range(
        self, user_id: str, start_date: datetime, end_date: datetime
    ) -> list[tuple[WorkspacePydantic, UserWorkspacePydantic]]:
        """Get all workspaces for a user that were active during the date range.

        Includes:
        - Active workspaces (status = ACTIVE)
        - Deleted workspaces that were deleted during or after the date range
        """
        async with self._session_manager.get_session() as session:
            # Get active workspaces
            active_query = (
                select(WorkspaceTable, UserWorkspaceTable)
                .join(UserWorkspaceTable)
                .where(UserWorkspaceTable.user_id == user_id)
                .where(WorkspaceTable.status == WorkspaceStatus.ACTIVE.value)
            )

            # Get deleted workspaces that were deleted during or after the date range
            deleted_query = (
                select(WorkspaceTable, UserWorkspaceTable)
                .join(UserWorkspaceTable)
                .where(UserWorkspaceTable.user_id == user_id)
                .where(WorkspaceTable.status == WorkspaceStatus.DELETED.value)
                .where(WorkspaceTable.deleted_at >= start_date)
            )

            # Combine both queries
            active_result = await session.execute(active_query)
            deleted_result = await session.execute(deleted_query)

            workspaces = []
            for workspace, membership in active_result.all():
                workspaces.append(
                    (
                        self._to_pydantic(workspace),
                        membership.to_pydantic(UserWorkspacePydantic),
                    )
                )
            for workspace, membership in deleted_result.all():
                workspaces.append(
                    (
                        self._to_pydantic(workspace),
                        membership.to_pydantic(UserWorkspacePydantic),
                    )
                )

            return workspaces

    async def get_active_workspace_count(self, user_id: str) -> int:
        """Count active workspaces for a user using a single COUNT query."""
        async with self._session_manager.get_session() as session:
            query = (
                select(func.count(WorkspaceTable.id))
                .join(UserWorkspaceTable)
                .where(UserWorkspaceTable.user_id == user_id)
                .where(WorkspaceTable.status == WorkspaceStatus.ACTIVE.value)
            )
            result = await session.execute(query)
            return result.scalar() or 0
