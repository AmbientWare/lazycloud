"""Tests for invitations API routes."""

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


class TestGetPendingInvitations:
    """Tests for GET /v1/invitations/pending."""

    async def test_get_pending_invitations(
        self, client: AsyncClient, api_db: Database, api_user: UserPydantic
    ):
        """User can see their pending invitations."""
        workspace = await api_db.workspaces.create(make_workspace())
        await api_db.invitations.create(
            make_invitation(str(workspace.id), str(api_user.id), email=api_user.email)
        )

        response = await client.get("/v1/invitations/pending")

        assert response.status_code == 200
        invitations = response.json()
        assert len(invitations) == 1
        assert invitations[0]["workspace_id"] == str(workspace.id)


class TestAcceptInvitation:
    """Tests for POST /v1/invitations/{id}/accept."""

    async def test_accept_invitation_success(
        self, client: AsyncClient, api_db: Database, api_user: UserPydantic
    ):
        """User can accept invitation."""
        workspace = await api_db.workspaces.create(make_workspace())
        owner = await api_db.users.create(make_user("owner"))
        await api_db.user_workspaces.create(
            make_user_workspace(owner.id, workspace.id, WorkspaceRole.OWNER)
        )

        invitation = await api_db.invitations.create(
            make_invitation(str(workspace.id), str(owner.id), email=api_user.email)
        )

        response = await client.post(f"/v1/invitations/{invitation.id}/accept")

        assert response.status_code == 200
        assert response.json()["success"] is True

    async def test_accept_invitation_wrong_user_fails(
        self, client: AsyncClient, api_db: Database, api_user: UserPydantic
    ):
        """User cannot accept invitation for different email."""
        workspace = await api_db.workspaces.create(make_workspace())
        owner = await api_db.users.create(make_user("owner"))
        invitation = await api_db.invitations.create(
            make_invitation(str(workspace.id), str(owner.id), email="other@example.com")
        )

        response = await client.post(f"/v1/invitations/{invitation.id}/accept")

        assert response.status_code == 403


class TestDeclineInvitation:
    """Tests for POST /v1/invitations/{id}/decline."""

    async def test_decline_invitation_success(
        self, client: AsyncClient, api_db: Database, api_user: UserPydantic
    ):
        """User can decline invitation."""
        workspace = await api_db.workspaces.create(make_workspace())
        owner = await api_db.users.create(make_user("owner"))
        invitation = await api_db.invitations.create(
            make_invitation(str(workspace.id), str(owner.id), email=api_user.email)
        )

        response = await client.post(f"/v1/invitations/{invitation.id}/decline")

        assert response.status_code == 200
        assert response.json()["success"] is True
