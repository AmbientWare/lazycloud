"""Tests for WorkspaceInvitationService database operations."""

from datetime import datetime, timedelta, timezone

from backend.database import Database
from backend.database.models import WorkspaceRole

from tests.fixtures.database import (
    make_invitation,
    make_user,
    make_user_workspace,
    make_workspace,
    requires_db,
)


@requires_db
class TestInvitationServiceCRUD:
    """Test basic CRUD operations for WorkspaceInvitationService."""

    async def test_create_invitation(self, db: Database):
        """Test creating an invitation."""
        user = await db.users.create(make_user())
        workspace = await db.workspaces.create(make_workspace())
        await db.user_workspaces.create(make_user_workspace(user.id, workspace.id))

        invitation = make_invitation(workspace.id, user.id, email="invitee@example.com")
        created = await db.invitations.create(invitation)

        assert created.id is not None
        assert created.workspace_id == workspace.id
        assert created.email == "invitee@example.com"
        assert created.role == WorkspaceRole.MEMBER
        assert created.invited_by_user_id == user.id
        assert created.token is not None
        assert created.expires_at is not None

    async def test_create_invitation_with_admin_role(self, db: Database):
        """Test creating an invitation with admin role."""
        user = await db.users.create(make_user())
        workspace = await db.workspaces.create(make_workspace())
        await db.user_workspaces.create(make_user_workspace(user.id, workspace.id))

        invitation = make_invitation(workspace.id, user.id, role=WorkspaceRole.ADMIN)
        created = await db.invitations.create(invitation)

        assert created.role == WorkspaceRole.ADMIN

    async def test_get_invitation_by_id(self, db: Database):
        """Test retrieving an invitation by ID."""
        user = await db.users.create(make_user())
        workspace = await db.workspaces.create(make_workspace())
        await db.user_workspaces.create(make_user_workspace(user.id, workspace.id))

        invitation = make_invitation(workspace.id, user.id)
        created = await db.invitations.create(invitation)

        retrieved = await db.invitations.get_by_id(created.id)

        assert retrieved is not None
        assert retrieved.id == created.id
        assert retrieved.email == created.email

    async def test_delete_invitation(self, db: Database):
        """Test deleting an invitation."""
        user = await db.users.create(make_user())
        workspace = await db.workspaces.create(make_workspace())
        await db.user_workspaces.create(make_user_workspace(user.id, workspace.id))

        invitation = make_invitation(workspace.id, user.id)
        created = await db.invitations.create(invitation)

        await db.invitations.delete(created.id)

        retrieved = await db.invitations.get_by_id(created.id)
        assert retrieved is None


@requires_db
class TestInvitationServiceQueries:
    """Test custom query methods for WorkspaceInvitationService."""

    async def test_get_by_token(self, db: Database):
        """Test retrieving an invitation by token."""
        user = await db.users.create(make_user())
        workspace = await db.workspaces.create(make_workspace())
        await db.user_workspaces.create(make_user_workspace(user.id, workspace.id))

        invitation = make_invitation(workspace.id, user.id)
        created = await db.invitations.create(invitation)

        retrieved = await db.invitations.get_by_token(created.token)

        assert retrieved is not None
        assert retrieved.id == created.id
        assert retrieved.token == created.token

    async def test_get_by_token_not_found(self, db: Database):
        """Test get_by_token returns None for non-existent token."""
        result = await db.invitations.get_by_token("non_existent_token")
        assert result is None

    async def test_get_by_workspace(self, db: Database):
        """Test retrieving all invitations for a workspace."""
        user = await db.users.create(make_user())
        workspace = await db.workspaces.create(make_workspace())
        await db.user_workspaces.create(make_user_workspace(user.id, workspace.id))

        await db.invitations.create(
            make_invitation(workspace.id, user.id, email="invite1@example.com")
        )
        await db.invitations.create(
            make_invitation(workspace.id, user.id, email="invite2@example.com")
        )
        await db.invitations.create(
            make_invitation(workspace.id, user.id, email="invite3@example.com")
        )

        invitations = await db.invitations.get_by_workspace(workspace.id)

        assert len(invitations) == 3
        emails = {i.email for i in invitations}
        assert "invite1@example.com" in emails
        assert "invite2@example.com" in emails
        assert "invite3@example.com" in emails

    async def test_get_by_workspace_and_email(self, db: Database):
        """Test retrieving invitation by email and workspace."""
        user = await db.users.create(make_user())
        workspace = await db.workspaces.create(make_workspace())
        await db.user_workspaces.create(make_user_workspace(user.id, workspace.id))

        await db.invitations.create(
            make_invitation(workspace.id, user.id, email="specific@example.com")
        )

        invitation = await db.invitations.get_by_workspace_and_email(
            workspace_id=workspace.id, email="specific@example.com"
        )

        assert invitation is not None
        assert invitation.email == "specific@example.com"

    async def test_get_by_workspace_and_email_not_found(self, db: Database):
        """Test get_by_workspace_and_email returns None when not found."""
        workspace = await db.workspaces.create(make_workspace())

        result = await db.invitations.get_by_workspace_and_email(
            workspace_id=workspace.id, email="nonexistent@example.com"
        )

        assert result is None


@requires_db
class TestInvitationServiceAcceptance:
    """Test invitation acceptance (marks accepted_at)."""

    async def test_accept_invitation(self, db: Database):
        """Test marking an invitation as accepted."""
        owner = await db.users.create(make_user("owner"))
        workspace = await db.workspaces.create(make_workspace())
        await db.user_workspaces.create(make_user_workspace(owner.id, workspace.id))

        invitation = make_invitation(workspace.id, owner.id)
        created = await db.invitations.create(invitation)
        assert created.accepted_at is None

        accepted = await db.invitations.accept_invitation(invitation_id=created.id)

        assert accepted is not None
        assert accepted.accepted_at is not None
        assert accepted.id == created.id

    async def test_accept_invitation_invalid_id(self, db: Database):
        """Test accepting with invalid ID returns None."""
        result = await db.invitations.accept_invitation(
            invitation_id="00000000-0000-0000-0000-000000000000"
        )

        assert result is None


@requires_db
class TestInvitationServiceExpiration:
    """Test invitation expiration handling."""

    async def test_create_invitation_with_custom_expiration(self, db: Database):
        """Test creating invitation with custom expiration."""
        user = await db.users.create(make_user())
        workspace = await db.workspaces.create(make_workspace())
        await db.user_workspaces.create(make_user_workspace(user.id, workspace.id))

        invitation = make_invitation(workspace.id, user.id, expires_in_days=30)
        created = await db.invitations.create(invitation)

        expected_min = datetime.now(timezone.utc) + timedelta(days=29)
        assert created.expires_at > expected_min

    async def test_delete_expired(self, db: Database):
        """Test deleting expired invitations."""
        user = await db.users.create(make_user())
        workspace = await db.workspaces.create(make_workspace())
        await db.user_workspaces.create(make_user_workspace(user.id, workspace.id))

        expired = make_invitation(workspace.id, user.id, expires_in_days=-1)
        valid = make_invitation(workspace.id, user.id, expires_in_days=7)

        await db.invitations.create(expired)
        await db.invitations.create(valid)

        deleted_count = await db.invitations.delete_expired()

        assert deleted_count >= 1

        invitations = await db.invitations.get_by_workspace(workspace.id)

        assert len(invitations) >= 1


@requires_db
class TestInvitationServiceMultipleWorkspaces:
    """Test invitations across multiple workspaces."""

    async def test_invitations_isolated_per_workspace(self, db: Database):
        """Test that invitations are isolated per workspace."""
        user = await db.users.create(make_user())
        ws1 = await db.workspaces.create(make_workspace())
        ws2 = await db.workspaces.create(make_workspace())

        await db.user_workspaces.create(make_user_workspace(user.id, ws1.id))
        await db.user_workspaces.create(make_user_workspace(user.id, ws2.id))

        await db.invitations.create(
            make_invitation(ws1.id, user.id, email="ws1@example.com")
        )
        await db.invitations.create(
            make_invitation(ws2.id, user.id, email="ws2@example.com")
        )

        ws1_invites = await db.invitations.get_by_workspace(ws1.id)
        ws2_invites = await db.invitations.get_by_workspace(ws2.id)

        assert len(ws1_invites) == 1
        assert len(ws2_invites) == 1
        assert ws1_invites[0].email == "ws1@example.com"
        assert ws2_invites[0].email == "ws2@example.com"

    async def test_same_email_can_be_invited_to_multiple_workspaces(self, db: Database):
        """Test that the same email can be invited to different workspaces."""
        user = await db.users.create(make_user())
        ws1 = await db.workspaces.create(make_workspace())
        ws2 = await db.workspaces.create(make_workspace())

        await db.user_workspaces.create(make_user_workspace(user.id, ws1.id))
        await db.user_workspaces.create(make_user_workspace(user.id, ws2.id))

        shared_email = "multi@example.com"

        await db.invitations.create(
            make_invitation(ws1.id, user.id, email=shared_email)
        )
        await db.invitations.create(
            make_invitation(ws2.id, user.id, email=shared_email)
        )

        ws1_invite = await db.invitations.get_by_workspace_and_email(
            workspace_id=ws1.id, email=shared_email
        )
        ws2_invite = await db.invitations.get_by_workspace_and_email(
            workspace_id=ws2.id, email=shared_email
        )

        assert ws1_invite is not None
        assert ws2_invite is not None
        assert ws1_invite.workspace_id == ws1.id
        assert ws2_invite.workspace_id == ws2.id
