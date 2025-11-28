"""Tests for WorkspaceService database operations."""

from datetime import datetime, timedelta, timezone

from backend.database import Database
from backend.database.workspaces import WorkspaceStatus
from models.workspaces import WorkspaceRole

from tests.fixtures.database import (
    make_user,
    make_user_workspace,
    make_workspace,
    requires_db,
)


@requires_db
class TestWorkspaceServiceCRUD:
    """Test basic CRUD operations for WorkspaceService."""

    async def test_create_workspace(self, db: Database):
        """Test creating a workspace."""
        workspace = make_workspace()
        created = await db.workspaces.create(workspace)

        assert created.id is not None
        assert created.name == workspace.name
        assert created.is_personal is False
        assert created.status == WorkspaceStatus.ACTIVE
        assert created.deleted_at is None
        assert created.created_at is not None
        assert created.updated_at is not None

    async def test_create_personal_workspace(self, db: Database):
        """Test creating a personal workspace."""
        workspace = make_workspace(name="Personal", is_personal=True)
        created = await db.workspaces.create(workspace)

        assert created.is_personal is True
        assert created.name == "Personal"

    async def test_get_workspace_by_id(self, db: Database):
        """Test retrieving a workspace by ID."""
        workspace = make_workspace()
        created = await db.workspaces.create(workspace)

        retrieved = await db.workspaces.get_by_id(created.id)

        assert retrieved is not None
        assert retrieved.id == created.id
        assert retrieved.name == created.name

    async def test_get_workspace_by_id_not_found(self, db: Database):
        """Test retrieving a non-existent workspace returns None."""
        result = await db.workspaces.get_by_id("00000000-0000-0000-0000-000000000000")
        assert result is None

    async def test_update_workspace(self, db: Database):
        """Test updating a workspace."""
        workspace = make_workspace()
        created = await db.workspaces.create(workspace)

        created.name = "Updated Workspace Name"
        updated = await db.workspaces.update(created)

        assert updated is not None
        assert updated.name == "Updated Workspace Name"

    async def test_delete_workspace(self, db: Database):
        """Test deleting a workspace."""
        workspace = make_workspace()
        created = await db.workspaces.create(workspace)

        await db.workspaces.delete(created.id)

        retrieved = await db.workspaces.get_by_id(created.id)
        assert retrieved is None


@requires_db
class TestWorkspaceServiceStatus:
    """Test workspace status operations."""

    async def test_update_status_to_inactive(self, db: Database):
        """Test updating workspace status to inactive."""
        workspace = make_workspace()
        created = await db.workspaces.create(workspace)

        updated = await db.workspaces.update_status(
            created.id, WorkspaceStatus.INACTIVE
        )

        assert updated is not None
        assert updated.status == WorkspaceStatus.INACTIVE
        assert updated.deleted_at is None

    async def test_update_status_to_deleted_sets_deleted_at(self, db: Database):
        """Test that setting status to DELETED sets deleted_at timestamp."""
        workspace = make_workspace()
        created = await db.workspaces.create(workspace)

        updated = await db.workspaces.update_status(created.id, WorkspaceStatus.DELETED)

        assert updated is not None
        assert updated.status == WorkspaceStatus.DELETED
        assert updated.deleted_at is not None

    async def test_soft_deleted_workspace_excluded_by_default(self, db: Database):
        """Test that soft-deleted workspaces are excluded from normal queries."""
        workspace = make_workspace()
        created = await db.workspaces.create(workspace)

        await db.workspaces.update_status(created.id, WorkspaceStatus.DELETED)

        retrieved = await db.workspaces.get_by_id(created.id)
        assert retrieved is None

        retrieved_with_deleted = await db.workspaces.get_by_id(
            created.id, include_deleted=True
        )
        assert retrieved_with_deleted is not None
        assert retrieved_with_deleted.status == WorkspaceStatus.DELETED


@requires_db
class TestWorkspaceServiceUserQueries:
    """Test user-workspace query methods."""

    async def test_get_personal_workspace(self, db: Database):
        """Test retrieving a user's personal workspace."""
        user = await db.users.create(make_user())
        personal_ws = await db.workspaces.create(
            make_workspace(name="Personal", is_personal=True)
        )
        await db.user_workspaces.create(make_user_workspace(user.id, personal_ws.id))

        retrieved = await db.workspaces.get_personal_workspace(user.id)

        assert retrieved is not None
        assert retrieved.is_personal is True

    async def test_get_personal_workspace_not_found(self, db: Database):
        """Test that get_personal_workspace returns None when no personal workspace."""
        user = await db.users.create(make_user())

        result = await db.workspaces.get_personal_workspace(user.id)
        assert result is None

    async def test_get_user_workspaces_with_membership(self, db: Database):
        """Test retrieving all workspaces for a user with membership info."""
        user = await db.users.create(make_user())

        ws1 = await db.workspaces.create(make_workspace())
        ws2 = await db.workspaces.create(make_workspace())

        await db.user_workspaces.create(
            make_user_workspace(user.id, ws1.id, WorkspaceRole.OWNER)
        )
        await db.user_workspaces.create(
            make_user_workspace(user.id, ws2.id, WorkspaceRole.MEMBER)
        )

        results = await db.workspaces.get_user_workspaces_with_membership(user.id)

        assert len(results) == 2
        workspace_ids = {ws.id for ws, _ in results}
        assert ws1.id in workspace_ids
        assert ws2.id in workspace_ids

    async def test_get_active_workspace_count(self, db: Database):
        """Test counting active workspaces for a user."""
        user = await db.users.create(make_user())

        for _ in range(3):
            ws = await db.workspaces.create(make_workspace())
            await db.user_workspaces.create(make_user_workspace(user.id, ws.id))

        count = await db.workspaces.get_active_workspace_count(user.id)
        assert count == 3


@requires_db
class TestWorkspaceServiceOwnership:
    """Test workspace ownership operations."""

    async def test_get_owner(self, db: Database):
        """Test getting the owner membership for a workspace."""
        user = await db.users.create(make_user())
        ws = await db.workspaces.create(make_workspace())
        await db.user_workspaces.create(
            make_user_workspace(user.id, ws.id, WorkspaceRole.OWNER)
        )

        owner = await db.workspaces.get_owner(ws.id)

        assert owner is not None
        assert owner.user_id == user.id
        assert owner.role == WorkspaceRole.OWNER

    async def test_get_owner_user(self, db: Database):
        """Test getting the owner user directly for a workspace."""
        user = await db.users.create(make_user())
        ws = await db.workspaces.create(make_workspace())
        await db.user_workspaces.create(
            make_user_workspace(user.id, ws.id, WorkspaceRole.OWNER)
        )

        owner_user = await db.workspaces.get_owner_user(ws.id)

        assert owner_user is not None
        assert owner_user.id == user.id
        assert owner_user.email == user.email

    async def test_transfer_ownership(self, db: Database):
        """Test transferring workspace ownership."""
        owner = await db.users.create(make_user("owner"))
        new_owner = await db.users.create(make_user("new_owner"))
        ws = await db.workspaces.create(make_workspace())

        await db.user_workspaces.create(
            make_user_workspace(owner.id, ws.id, WorkspaceRole.OWNER)
        )
        await db.user_workspaces.create(
            make_user_workspace(new_owner.id, ws.id, WorkspaceRole.MEMBER)
        )

        old_membership, new_membership = await db.workspaces.transfer_ownership(
            ws.id, owner.id, new_owner.id
        )

        assert old_membership is not None
        assert new_membership is not None
        assert old_membership.role == WorkspaceRole.ADMIN
        assert new_membership.role == WorkspaceRole.OWNER

    async def test_transfer_ownership_missing_member(self, db: Database):
        """Test transfer fails when new owner is not a member."""
        owner = await db.users.create(make_user("owner"))
        non_member = await db.users.create(make_user("non_member"))
        ws = await db.workspaces.create(make_workspace())

        await db.user_workspaces.create(
            make_user_workspace(owner.id, ws.id, WorkspaceRole.OWNER)
        )

        old_membership, new_membership = await db.workspaces.transfer_ownership(
            ws.id, owner.id, non_member.id
        )

        assert old_membership is None
        assert new_membership is None


@requires_db
class TestWorkspaceServiceDateQueries:
    """Test date-range query methods."""

    async def test_get_active_workspaces(self, db: Database):
        """Test retrieving all active workspaces."""
        await db.workspaces.create(make_workspace())
        await db.workspaces.create(make_workspace())

        active = await db.workspaces.get_active_workspaces()

        assert isinstance(active, list)
        assert len(active) >= 2

    async def test_get_deleted_in_range(self, db: Database):
        """Test retrieving workspaces deleted within a date range."""
        ws = await db.workspaces.create(make_workspace())
        await db.workspaces.update_status(ws.id, WorkspaceStatus.DELETED)

        now = datetime.now(timezone.utc)
        start = now - timedelta(hours=1)
        end = now + timedelta(hours=1)

        deleted = await db.workspaces.get_deleted_in_range(start, end)

        assert isinstance(deleted, list)

    async def test_get_user_workspaces_active_during_range(self, db: Database):
        """Test retrieving workspaces active during a date range."""
        user = await db.users.create(make_user())
        ws = await db.workspaces.create(make_workspace())
        await db.user_workspaces.create(make_user_workspace(user.id, ws.id))

        now = datetime.now(timezone.utc)
        start = now - timedelta(hours=1)
        end = now + timedelta(hours=1)

        results = await db.workspaces.get_user_workspaces_active_during_range(
            user.id, start, end
        )

        assert len(results) >= 1
