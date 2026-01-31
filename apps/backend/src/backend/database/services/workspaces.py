import asyncio
from datetime import datetime, timezone

from sqlalchemy import func, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select

from backend.database.models import (
    UserInDb,
    UserWorkspaceInDb,
    WorkspaceInDb,
    WorkspaceRole,
    WorkspaceStatus,
)
from backend.database.services.base import DatabaseService
from backend.database.tables import (
    UserTable,
    UserWorkspaceTable,
    WorkspaceTable,
)


class WorkspaceService(DatabaseService[WorkspaceTable, WorkspaceInDb]):
    """Service layer for workspace operations."""

    def __init__(self, session: AsyncSession):
        super().__init__(WorkspaceTable, WorkspaceInDb, session)

    async def get_user_workspaces_with_membership(
        self, user_id: str, status: WorkspaceStatus = WorkspaceStatus.ACTIVE
    ) -> list[tuple[WorkspaceInDb, UserWorkspaceInDb]]:
        """Get all workspaces for a user with membership info in a single query."""
        query = (
            select(WorkspaceTable, UserWorkspaceTable)
            .join(UserWorkspaceTable)
            .where(UserWorkspaceTable.user_id == user_id)
            .where(WorkspaceTable.status == status.value)
        )
        result = await self._session.execute(query)
        return [
            (self._to_pydantic(workspace), membership.to_pydantic(UserWorkspaceInDb))
            for workspace, membership in result.all()
        ]

    async def get_personal_workspace(self, user_id: str) -> WorkspaceInDb | None:
        """Get a user's personal workspace."""
        query = (
            select(WorkspaceTable)
            .join(UserWorkspaceTable)
            .where(UserWorkspaceTable.user_id == user_id)
            .where(WorkspaceTable.is_personal.is_(True))
        )
        result = await self._session.execute(query)
        workspace = result.scalar_one_or_none()
        return self._to_pydantic(workspace) if workspace else None

    async def get_owner(self, workspace_id: str) -> UserWorkspaceInDb | None:
        """Get the owner membership for a workspace."""
        query = (
            select(UserWorkspaceTable)
            .where(UserWorkspaceTable.workspace_id == workspace_id)
            .where(UserWorkspaceTable.role == WorkspaceRole.OWNER.value)
        )
        result = await self._session.execute(query)
        membership = result.scalar_one_or_none()
        return membership.to_pydantic(UserWorkspaceInDb) if membership else None

    async def get_owner_user(self, workspace_id: str) -> UserInDb | None:
        """Get the owner user for a workspace in a single query."""
        query = (
            select(UserTable)
            .join(UserWorkspaceTable, UserTable.id == UserWorkspaceTable.user_id)
            .where(UserWorkspaceTable.workspace_id == workspace_id)
            .where(UserWorkspaceTable.role == WorkspaceRole.OWNER.value)
        )
        result = await self._session.execute(query)
        user = result.scalar_one_or_none()
        return user.to_pydantic(UserInDb) if user else None

    async def transfer_ownership(
        self,
        workspace_id: str,
        current_owner_id: str,
        new_owner_id: str,
    ) -> tuple[UserWorkspaceInDb | None, UserWorkspaceInDb | None]:
        """Transfer ownership from one user to another."""
        query = (
            select(UserWorkspaceTable)
            .where(UserWorkspaceTable.workspace_id == workspace_id)
            .where(UserWorkspaceTable.user_id.in_([current_owner_id, new_owner_id]))
        )
        result = await self._session.execute(query)
        memberships = result.scalars().all()

        current_owner_membership = None
        new_owner_membership = None

        for membership in memberships:
            if str(membership.user_id) == current_owner_id:
                current_owner_membership = membership
            elif str(membership.user_id) == new_owner_id:
                new_owner_membership = membership

        if not current_owner_membership or not new_owner_membership:
            return (None, None)

        current_owner_membership.role = WorkspaceRole.ADMIN.value
        new_owner_membership.role = WorkspaceRole.OWNER.value

        await self._session.flush()
        await self._session.refresh(current_owner_membership)
        await self._session.refresh(new_owner_membership)

        return (
            current_owner_membership.to_pydantic(UserWorkspaceInDb),
            new_owner_membership.to_pydantic(UserWorkspaceInDb),
        )

    async def update_status(
        self, workspace_id: str, status: WorkspaceStatus
    ) -> WorkspaceInDb | None:
        """Update the status of a workspace."""
        update_values: dict = {
            "status": status.value,
            "updated_at": datetime.now(timezone.utc),
        }
        if status == WorkspaceStatus.DELETED:
            update_values["deleted_at"] = datetime.now(timezone.utc)

        stmt = (
            update(WorkspaceTable)
            .where(WorkspaceTable.id == workspace_id)
            .values(**update_values)
            .returning(WorkspaceTable)
        )
        result = await self._session.execute(
            stmt, execution_options={"populate_existing": True}
        )
        updated_workspace = result.scalar_one_or_none()
        return self._to_pydantic(updated_workspace) if updated_workspace else None

    async def get_active_workspaces(self) -> list[WorkspaceInDb]:
        """Get all active workspaces."""
        return await self.find({"status": WorkspaceStatus.ACTIVE.value})

    async def get_active_workspaces_before(
        self, before_date: datetime
    ) -> list[WorkspaceInDb]:
        """Get all active workspaces created before a specific date."""
        query = (
            select(WorkspaceTable)
            .where(WorkspaceTable.status == WorkspaceStatus.ACTIVE.value)
            .where(WorkspaceTable.created_at < before_date)
        )
        result = await self._session.execute(query)
        return [self._to_pydantic(ws) for ws in result.scalars().all()]

    async def get_deleted_in_range(
        self, start_date: datetime, end_date: datetime
    ) -> list[WorkspaceInDb]:
        """Get all workspaces deleted within a date range (inclusive)."""
        query = (
            select(WorkspaceTable)
            .where(WorkspaceTable.status == WorkspaceStatus.DELETED.value)
            .where(WorkspaceTable.deleted_at >= start_date)
            .where(WorkspaceTable.deleted_at <= end_date)
        )
        result = await self._session.execute(query)
        return [self._to_pydantic(ws) for ws in result.scalars().all()]

    async def get_user_workspaces_active_during_range(
        self, user_id: str, start_date: datetime, end_date: datetime
    ) -> list[tuple[WorkspaceInDb, UserWorkspaceInDb]]:
        """Get all workspaces for a user that were active during the date range.

        Includes:
        - Active workspaces (status = ACTIVE)
        - Deleted workspaces that were deleted during or after the date range
        """
        active_query = (
            select(WorkspaceTable, UserWorkspaceTable)
            .join(UserWorkspaceTable)
            .where(UserWorkspaceTable.user_id == user_id)
            .where(WorkspaceTable.status == WorkspaceStatus.ACTIVE.value)
        )

        deleted_query = (
            select(WorkspaceTable, UserWorkspaceTable)
            .join(UserWorkspaceTable)
            .where(UserWorkspaceTable.user_id == user_id)
            .where(WorkspaceTable.status == WorkspaceStatus.DELETED.value)
            .where(WorkspaceTable.deleted_at >= start_date)
        )

        active_result, deleted_result = await asyncio.gather(
            self._session.execute(active_query),
            self._session.execute(deleted_query),
        )

        workspaces = [
            (self._to_pydantic(ws), membership.to_pydantic(UserWorkspaceInDb))
            for ws, membership in active_result.all()
        ]
        workspaces.extend(
            (self._to_pydantic(ws), membership.to_pydantic(UserWorkspaceInDb))
            for ws, membership in deleted_result.all()
        )

        return workspaces

    async def get_active_workspace_count(self, user_id: str) -> int:
        """Count active workspaces for a user."""
        query = (
            select(func.count(WorkspaceTable.id))
            .join(UserWorkspaceTable)
            .where(UserWorkspaceTable.user_id == user_id)
            .where(WorkspaceTable.status == WorkspaceStatus.ACTIVE.value)
        )
        result = await self._session.execute(query)
        return result.scalar() or 0
