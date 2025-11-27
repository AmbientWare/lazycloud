"""Tests for UserWorkspaceService database operations."""

from lazycloud_api.database import Database
from lazycloud_api.tests.fixtures.database import (
    make_user,
    make_user_workspace,
    make_workspace,
    requires_db,
)
from shared.models.workspaces import UserWorkspaceStatus, WorkspaceRole


@requires_db
class TestUserWorkspaceServiceCRUD:
    """Test basic CRUD operations for UserWorkspaceService."""

    async def test_create_membership(self, db: Database):
        """Test creating a user-workspace membership."""
        user = await db.users.create(make_user())
        workspace = await db.workspaces.create(make_workspace())

        membership = make_user_workspace(user.id, workspace.id, WorkspaceRole.OWNER)
        created = await db.user_workspaces.create(membership)

        assert created.user_id == user.id
        assert created.workspace_id == workspace.id
        assert created.role == WorkspaceRole.OWNER
        assert created.status == UserWorkspaceStatus.ACTIVE

    async def test_create_member_role(self, db: Database):
        """Test creating a membership with member role."""
        user = await db.users.create(make_user())
        workspace = await db.workspaces.create(make_workspace())

        membership = make_user_workspace(user.id, workspace.id, WorkspaceRole.MEMBER)
        created = await db.user_workspaces.create(membership)

        assert created.role == WorkspaceRole.MEMBER

    async def test_create_admin_role(self, db: Database):
        """Test creating a membership with admin role."""
        user = await db.users.create(make_user())
        workspace = await db.workspaces.create(make_workspace())

        membership = make_user_workspace(user.id, workspace.id, WorkspaceRole.ADMIN)
        created = await db.user_workspaces.create(membership)

        assert created.role == WorkspaceRole.ADMIN


@requires_db
class TestUserWorkspaceServiceQueries:
    """Test query methods for UserWorkspaceService."""

    async def test_get_by_user_and_workspace(self, db: Database):
        """Test retrieving membership by user and workspace."""
        user = await db.users.create(make_user())
        workspace = await db.workspaces.create(make_workspace())
        membership = make_user_workspace(user.id, workspace.id)
        await db.user_workspaces.create(membership)

        retrieved = await db.user_workspaces.get_by_user_and_workspace(
            user.id, workspace.id
        )

        assert retrieved is not None
        assert retrieved.user_id == user.id
        assert retrieved.workspace_id == workspace.id

    async def test_get_by_user_and_workspace_not_found(self, db: Database):
        """Test that non-existent membership returns None."""
        user = await db.users.create(make_user())
        workspace = await db.workspaces.create(make_workspace())

        result = await db.user_workspaces.get_by_user_and_workspace(
            user.id, workspace.id
        )

        assert result is None

    async def test_get_user_memberships(self, db: Database):
        """Test retrieving all memberships for a user."""
        user = await db.users.create(make_user())
        ws1 = await db.workspaces.create(make_workspace())
        ws2 = await db.workspaces.create(make_workspace())
        ws3 = await db.workspaces.create(make_workspace())

        await db.user_workspaces.create(
            make_user_workspace(user.id, ws1.id, WorkspaceRole.OWNER)
        )
        await db.user_workspaces.create(
            make_user_workspace(user.id, ws2.id, WorkspaceRole.ADMIN)
        )
        await db.user_workspaces.create(
            make_user_workspace(user.id, ws3.id, WorkspaceRole.MEMBER)
        )

        memberships = await db.user_workspaces.get_user_memberships(user.id)

        assert len(memberships) == 3
        roles = {m.role for m in memberships}
        assert WorkspaceRole.OWNER in roles
        assert WorkspaceRole.ADMIN in roles
        assert WorkspaceRole.MEMBER in roles

    async def test_get_workspace_members(self, db: Database):
        """Test retrieving all members of a workspace."""
        user1 = await db.users.create(make_user("user1"))
        user2 = await db.users.create(make_user("user2"))
        user3 = await db.users.create(make_user("user3"))
        workspace = await db.workspaces.create(make_workspace())

        await db.user_workspaces.create(
            make_user_workspace(user1.id, workspace.id, WorkspaceRole.OWNER)
        )
        await db.user_workspaces.create(
            make_user_workspace(user2.id, workspace.id, WorkspaceRole.ADMIN)
        )
        await db.user_workspaces.create(
            make_user_workspace(user3.id, workspace.id, WorkspaceRole.MEMBER)
        )

        members = await db.user_workspaces.get_workspace_members(workspace.id)

        assert len(members) == 3
        user_ids = {m.user_id for m in members}
        assert user1.id in user_ids
        assert user2.id in user_ids
        assert user3.id in user_ids

    async def test_get_workspace_members_with_users(self, db: Database):
        """Test retrieving members with user information."""
        user1 = await db.users.create(make_user("user1"))
        user2 = await db.users.create(make_user("user2"))
        workspace = await db.workspaces.create(make_workspace())

        await db.user_workspaces.create(
            make_user_workspace(user1.id, workspace.id, WorkspaceRole.OWNER)
        )
        await db.user_workspaces.create(
            make_user_workspace(user2.id, workspace.id, WorkspaceRole.MEMBER)
        )

        results = await db.user_workspaces.get_workspace_members_with_users(
            workspace.id
        )

        assert len(results) == 2
        for membership, user in results:
            assert membership.user_id == user.id
            assert user.name is not None
            assert user.email is not None


@requires_db
class TestUserWorkspaceServiceUpdates:
    """Test update methods for UserWorkspaceService."""

    async def test_update_role(self, db: Database):
        """Test updating a member's role."""
        user = await db.users.create(make_user())
        workspace = await db.workspaces.create(make_workspace())
        await db.user_workspaces.create(
            make_user_workspace(user.id, workspace.id, WorkspaceRole.MEMBER)
        )

        updated = await db.user_workspaces.update_role(
            user.id, workspace.id, WorkspaceRole.ADMIN
        )

        assert updated is not None
        assert updated.role == WorkspaceRole.ADMIN

    async def test_update_role_non_existent_membership(self, db: Database):
        """Test updating role for non-existent membership returns None."""
        user = await db.users.create(make_user())
        workspace = await db.workspaces.create(make_workspace())

        result = await db.user_workspaces.update_role(
            user.id, workspace.id, WorkspaceRole.ADMIN
        )

        assert result is None

    async def test_update_status(self, db: Database):
        """Test updating a member's status."""
        user = await db.users.create(make_user())
        workspace = await db.workspaces.create(make_workspace())
        await db.user_workspaces.create(make_user_workspace(user.id, workspace.id))

        updated = await db.user_workspaces.update_status(
            user_id=user.id,
            workspace_id=workspace.id,
            status=UserWorkspaceStatus.SUSPENDED,
        )

        assert updated is not None
        assert updated.status == UserWorkspaceStatus.SUSPENDED

    async def test_update_status_non_existent_membership(self, db: Database):
        """Test updating status for non-existent membership returns None."""
        user = await db.users.create(make_user())
        workspace = await db.workspaces.create(make_workspace())

        result = await db.user_workspaces.update_status(
            user_id=user.id,
            workspace_id=workspace.id,
            status=UserWorkspaceStatus.SUSPENDED,
        )

        assert result is None


@requires_db
class TestUserWorkspaceServiceMultipleRoles:
    """Test scenarios with multiple workspace memberships."""

    async def test_user_can_have_different_roles_in_different_workspaces(
        self, db: Database
    ):
        """Test a user can have different roles in different workspaces."""
        user = await db.users.create(make_user())
        ws_owned = await db.workspaces.create(make_workspace())
        ws_admin = await db.workspaces.create(make_workspace())
        ws_member = await db.workspaces.create(make_workspace())

        await db.user_workspaces.create(
            make_user_workspace(user.id, ws_owned.id, WorkspaceRole.OWNER)
        )
        await db.user_workspaces.create(
            make_user_workspace(user.id, ws_admin.id, WorkspaceRole.ADMIN)
        )
        await db.user_workspaces.create(
            make_user_workspace(user.id, ws_member.id, WorkspaceRole.MEMBER)
        )

        memberships = await db.user_workspaces.get_user_memberships(user.id)

        role_map = {m.workspace_id: m.role for m in memberships}
        assert role_map[ws_owned.id] == WorkspaceRole.OWNER
        assert role_map[ws_admin.id] == WorkspaceRole.ADMIN
        assert role_map[ws_member.id] == WorkspaceRole.MEMBER

    async def test_workspace_can_have_multiple_members(self, db: Database):
        """Test a workspace can have multiple members with different roles."""
        owner = await db.users.create(make_user("owner"))
        admin1 = await db.users.create(make_user("admin1"))
        admin2 = await db.users.create(make_user("admin2"))
        member1 = await db.users.create(make_user("member1"))
        member2 = await db.users.create(make_user("member2"))
        workspace = await db.workspaces.create(make_workspace())

        await db.user_workspaces.create(
            make_user_workspace(owner.id, workspace.id, WorkspaceRole.OWNER)
        )
        await db.user_workspaces.create(
            make_user_workspace(admin1.id, workspace.id, WorkspaceRole.ADMIN)
        )
        await db.user_workspaces.create(
            make_user_workspace(admin2.id, workspace.id, WorkspaceRole.ADMIN)
        )
        await db.user_workspaces.create(
            make_user_workspace(member1.id, workspace.id, WorkspaceRole.MEMBER)
        )
        await db.user_workspaces.create(
            make_user_workspace(member2.id, workspace.id, WorkspaceRole.MEMBER)
        )

        members = await db.user_workspaces.get_workspace_members(workspace.id)

        assert len(members) == 5
        owner_count = sum(1 for m in members if m.role == WorkspaceRole.OWNER)
        admin_count = sum(1 for m in members if m.role == WorkspaceRole.ADMIN)
        member_count = sum(1 for m in members if m.role == WorkspaceRole.MEMBER)

        assert owner_count == 1
        assert admin_count == 2
        assert member_count == 2
