"""Tests for workspace members API routes."""

import pytest
from backend.database import Database
from backend.database.models import UserPydantic, WorkspaceRole
from httpx import AsyncClient

from tests.fixtures.database import (
    make_invitation,
    make_user,
    make_user_workspace,
    make_workspace,
    requires_db,
)

pytestmark = [pytest.mark.asyncio, requires_db]


class TestListMembers:
    """Tests for GET /v1/workspaces/{id}/members."""

    async def test_list_members_includes_active_members(
        self, client: AsyncClient, api_db: Database, api_user: UserPydantic
    ):
        """List includes all active workspace members."""
        workspace = await api_db.workspaces.create(make_workspace())
        await api_db.user_workspaces.create(
            make_user_workspace(api_user.id, workspace.id, WorkspaceRole.OWNER)
        )

        other_user = await api_db.users.create(make_user("other"))
        await api_db.user_workspaces.create(
            make_user_workspace(other_user.id, workspace.id, WorkspaceRole.MEMBER)
        )

        response = await client.get(f"/v1/workspaces/{workspace.id}/members")

        assert response.status_code == 200
        members = response.json()
        assert len(members) == 2
        user_ids = {m["user_id"] for m in members if m["user_id"]}
        assert str(api_user.id) in user_ids
        assert str(other_user.id) in user_ids
        # Verify roles are present
        roles = {m["role"] for m in members}
        assert WorkspaceRole.OWNER.value in roles
        assert WorkspaceRole.MEMBER.value in roles

    async def test_list_members_includes_pending_invitations(
        self, client: AsyncClient, api_db: Database, api_user: UserPydantic
    ):
        """List includes pending invitations."""
        workspace = await api_db.workspaces.create(make_workspace())
        await api_db.user_workspaces.create(
            make_user_workspace(api_user.id, workspace.id, WorkspaceRole.OWNER)
        )

        await api_db.invitations.create(
            make_invitation(
                str(workspace.id), str(api_user.id), email="invitee@example.com"
            )
        )

        response = await client.get(f"/v1/workspaces/{workspace.id}/members")

        assert response.status_code == 200
        members = response.json()
        assert len(members) == 2
        invited = [m for m in members if m["status"] == "invited"]
        assert len(invited) == 1
        assert invited[0]["email"] == "invitee@example.com"


class TestUpdateMemberRole:
    """Tests for PATCH /v1/workspaces/{id}/members/{user_id}/role."""

    async def test_update_member_role_success(
        self, client: AsyncClient, api_db: Database, api_user: UserPydantic
    ):
        """Successfully update member role."""
        workspace = await api_db.workspaces.create(make_workspace())
        await api_db.user_workspaces.create(
            make_user_workspace(api_user.id, workspace.id, WorkspaceRole.OWNER)
        )

        other_user = await api_db.users.create(make_user("other"))
        await api_db.user_workspaces.create(
            make_user_workspace(other_user.id, workspace.id, WorkspaceRole.MEMBER)
        )

        response = await client.patch(
            f"/v1/workspaces/{workspace.id}/members/{other_user.id}/role",
            json={"user_id": str(other_user.id), "role": WorkspaceRole.ADMIN.value},
        )

        assert response.status_code == 200
        data = response.json()
        assert data["role"] == WorkspaceRole.ADMIN.value

    async def test_update_member_role_requires_admin(
        self, client: AsyncClient, api_db: Database, api_user: UserPydantic
    ):
        """Only admins and owners can update roles."""
        workspace = await api_db.workspaces.create(make_workspace())
        await api_db.user_workspaces.create(
            make_user_workspace(api_user.id, workspace.id, WorkspaceRole.MEMBER)
        )

        other_user = await api_db.users.create(make_user("other"))
        await api_db.user_workspaces.create(
            make_user_workspace(other_user.id, workspace.id, WorkspaceRole.MEMBER)
        )

        response = await client.patch(
            f"/v1/workspaces/{workspace.id}/members/{other_user.id}/role",
            json={"user_id": str(other_user.id), "role": WorkspaceRole.ADMIN.value},
        )

        assert response.status_code == 403

    async def test_update_owner_role_fails(
        self, client: AsyncClient, api_db: Database, api_user: UserPydantic
    ):
        """Cannot change owner role."""
        workspace = await api_db.workspaces.create(make_workspace())
        await api_db.user_workspaces.create(
            make_user_workspace(api_user.id, workspace.id, WorkspaceRole.OWNER)
        )

        response = await client.patch(
            f"/v1/workspaces/{workspace.id}/members/{api_user.id}/role",
            json={"user_id": str(api_user.id), "role": WorkspaceRole.MEMBER.value},
        )

        assert response.status_code == 400


class TestRemoveMember:
    """Tests for DELETE /v1/workspaces/{id}/members/{user_id}."""

    async def test_remove_member_success(
        self, client: AsyncClient, api_db: Database, api_user: UserPydantic
    ):
        """Successfully remove a member."""
        workspace = await api_db.workspaces.create(make_workspace())
        await api_db.user_workspaces.create(
            make_user_workspace(api_user.id, workspace.id, WorkspaceRole.OWNER)
        )

        other_user = await api_db.users.create(make_user("other"))
        await api_db.user_workspaces.create(
            make_user_workspace(other_user.id, workspace.id, WorkspaceRole.MEMBER)
        )

        response = await client.delete(
            f"/v1/workspaces/{workspace.id}/members/{other_user.id}"
        )

        assert response.status_code == 200
        assert response.json()["success"] is True

    async def test_remove_owner_fails(
        self, client: AsyncClient, api_db: Database, api_user: UserPydantic
    ):
        """Cannot remove workspace owner."""
        workspace = await api_db.workspaces.create(make_workspace())
        await api_db.user_workspaces.create(
            make_user_workspace(api_user.id, workspace.id, WorkspaceRole.OWNER)
        )

        response = await client.delete(
            f"/v1/workspaces/{workspace.id}/members/{api_user.id}"
        )

        assert response.status_code == 400


class TestInviteUser:
    """Tests for POST /v1/workspaces/{id}/members/invite."""

    async def test_invite_user_success(
        self, client: AsyncClient, api_db: Database, api_user: UserPydantic
    ):
        """Successfully invite a user."""
        workspace = await api_db.workspaces.create(make_workspace())
        await api_db.user_workspaces.create(
            make_user_workspace(api_user.id, workspace.id, WorkspaceRole.OWNER)
        )

        response = await client.post(
            f"/v1/workspaces/{workspace.id}/members/invite",
            json={"email": "newuser@example.com", "role": WorkspaceRole.MEMBER.value},
        )

        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert "token" in data

    async def test_invite_user_requires_admin(
        self, client: AsyncClient, api_db: Database, api_user: UserPydantic
    ):
        """Only admins and owners can invite users."""
        workspace = await api_db.workspaces.create(make_workspace())
        await api_db.user_workspaces.create(
            make_user_workspace(api_user.id, workspace.id, WorkspaceRole.MEMBER)
        )

        response = await client.post(
            f"/v1/workspaces/{workspace.id}/members/invite",
            json={"email": "newuser@example.com", "role": WorkspaceRole.MEMBER.value},
        )

        assert response.status_code == 403


class TestLeaveWorkspace:
    """Tests for POST /v1/workspaces/{id}/members/leave."""

    async def test_leave_workspace_success(
        self, client: AsyncClient, api_db: Database, api_user: UserPydantic
    ):
        """Successfully leave a workspace."""
        workspace = await api_db.workspaces.create(make_workspace())
        await api_db.user_workspaces.create(
            make_user_workspace(api_user.id, workspace.id, WorkspaceRole.MEMBER)
        )

        response = await client.post(f"/v1/workspaces/{workspace.id}/members/leave")

        assert response.status_code == 200
        assert response.json()["success"] is True

    async def test_leave_workspace_as_owner_fails(
        self, client: AsyncClient, api_db: Database, api_user: UserPydantic
    ):
        """Owner cannot leave workspace."""
        workspace = await api_db.workspaces.create(make_workspace())
        await api_db.user_workspaces.create(
            make_user_workspace(api_user.id, workspace.id, WorkspaceRole.OWNER)
        )

        response = await client.post(f"/v1/workspaces/{workspace.id}/members/leave")

        assert response.status_code == 400
