"""Integration workflow tests that span multiple services."""

from datetime import datetime, timezone

from backend.database import Database
from backend.database.models import (
    DailyUsageStatus,
    UserWorkspaceStatus,
    WorkspaceRole,
    WorkspaceStatus,
)
from models.deployments import DeploymentStates

from tests.fixtures.database import (
    make_api_key,
    make_deployment,
    make_invitation,
    make_secret,
    make_user,
    make_user_workspace,
    make_workspace,
    requires_db,
)


@requires_db
class TestUserOnboardingWorkflow:
    """Test the complete user onboarding workflow."""

    async def test_user_onboarding_creates_personal_workspace(self, db: Database):
        """Test that user onboarding creates user with personal workspace."""
        user = await db.users.create(make_user())
        personal_ws = await db.workspaces.create(
            make_workspace(name=f"{user.name}'s Personal", is_personal=True)
        )
        await db.user_workspaces.create(
            make_user_workspace(user.id, personal_ws.id, WorkspaceRole.OWNER)
        )

        retrieved_user = await db.users.get_by_id(user.id)
        personal = await db.workspaces.get_personal_workspace(user.id)

        assert retrieved_user is not None
        assert personal is not None
        assert personal.is_personal is True

    async def test_user_onboarding_creates_api_key(self, db: Database):
        """Test that user onboarding can create initial API key."""
        user = await db.users.create(make_user())
        _ = await db.api_keys.create(make_api_key(user.id, name="Default Key"))

        keys = await db.api_keys.get_by_user_id(user.id)

        assert len(keys) == 1
        assert keys[0].name == "Default Key"

    async def test_complete_onboarding_workflow(self, db: Database):
        """Test complete user onboarding workflow."""
        user = await db.users.create(make_user())

        personal_ws = await db.workspaces.create(
            make_workspace(name=f"{user.name}'s Personal", is_personal=True)
        )
        await db.user_workspaces.create(
            make_user_workspace(user.id, personal_ws.id, WorkspaceRole.OWNER)
        )

        await db.api_keys.create(make_api_key(user.id, name="Default Key"))

        assert await db.users.exists(user.id)
        assert await db.workspaces.get_personal_workspace(user.id) is not None
        assert len(await db.api_keys.get_by_user_id(user.id)) == 1


@requires_db
class TestDeploymentLifecycleWorkflow:
    """Test the complete deployment lifecycle."""

    async def test_deployment_creation_to_deployed(self, db: Database):
        """Test deployment lifecycle from creation to deployed."""
        user = await db.users.create(make_user())
        workspace = await db.workspaces.create(make_workspace())
        await db.user_workspaces.create(make_user_workspace(user.id, workspace.id))

        deployment = make_deployment(
            workspace.id, name="my-app", state=DeploymentStates.PENDING
        )
        created = await db.compose_deployments.create(deployment)

        await db.compose_deployments.update_status(
            created.id, DeploymentStates.DEPLOYING, message="Building..."
        )

        await db.compose_deployments.update_status(
            created.id, DeploymentStates.DEPLOYED, message="Running"
        )

        final = await db.compose_deployments.get_by_id(created.id)

        assert final is not None
        assert final.state == DeploymentStates.DEPLOYED
        assert final.status_message == "Running"

    async def test_deployment_with_secrets(self, db: Database):
        """Test deployment with secrets."""
        user = await db.users.create(make_user())
        workspace = await db.workspaces.create(make_workspace())
        await db.user_workspaces.create(make_user_workspace(user.id, workspace.id))

        deployment = await db.compose_deployments.create(
            make_deployment(workspace.id, name="app-with-secrets")
        )

        await db.secrets.create(
            make_secret(deployment.id, key="DATABASE_URL", value="postgres://...")
        )
        await db.secrets.create(
            make_secret(deployment.id, key="API_KEY", value="secret123")
        )

        secrets = await db.secrets.get_secrets(deployment.id)

        assert len(secrets) == 2
        keys = {s.key for s in secrets}
        assert "DATABASE_URL" in keys
        assert "API_KEY" in keys

    async def test_deployment_soft_delete(self, db: Database):
        """Test deployment soft delete."""
        user = await db.users.create(make_user())
        workspace = await db.workspaces.create(make_workspace())
        await db.user_workspaces.create(make_user_workspace(user.id, workspace.id))

        deployment = await db.compose_deployments.create(make_deployment(workspace.id))

        deployment.deleted_at = datetime.now(timezone.utc)
        await db.compose_deployments.update(deployment)

        count = await db.compose_deployments.get_deployment_count(workspace.id)
        assert count == 0


@requires_db
class TestWorkspaceCollaborationWorkflow:
    """Test workspace collaboration workflows."""

    async def test_invite_and_accept_member(self, db: Database):
        """Test inviting and accepting a new member."""
        owner = await db.users.create(make_user("owner"))
        workspace = await db.workspaces.create(make_workspace())
        await db.user_workspaces.create(
            make_user_workspace(owner.id, workspace.id, WorkspaceRole.OWNER)
        )

        new_member = await db.users.create(make_user("new_member"))

        invitation = make_invitation(
            workspace.id,
            owner.id,
            email=new_member.email,
            role=WorkspaceRole.MEMBER,
        )
        created_invite = await db.invitations.create(invitation)

        # Accept the invitation (marks accepted_at)
        await db.invitations.accept_invitation(invitation_id=created_invite.id)

        # Manually create the membership (as the app would do)
        membership = await db.user_workspaces.create(
            make_user_workspace(new_member.id, workspace.id, WorkspaceRole.MEMBER)
        )

        assert membership is not None
        assert membership.role == WorkspaceRole.MEMBER

        members = await db.user_workspaces.get_workspace_members(workspace.id)
        assert len(members) == 2

    async def test_promote_member_to_admin(self, db: Database):
        """Test promoting a member to admin."""
        owner = await db.users.create(make_user("owner"))
        member = await db.users.create(make_user("member"))
        workspace = await db.workspaces.create(make_workspace())

        await db.user_workspaces.create(
            make_user_workspace(owner.id, workspace.id, WorkspaceRole.OWNER)
        )
        await db.user_workspaces.create(
            make_user_workspace(member.id, workspace.id, WorkspaceRole.MEMBER)
        )

        updated = await db.user_workspaces.update_role(
            member.id, workspace.id, WorkspaceRole.ADMIN
        )

        assert updated is not None
        assert updated.role == WorkspaceRole.ADMIN

    async def test_transfer_ownership(self, db: Database):
        """Test transferring workspace ownership."""
        original_owner = await db.users.create(make_user("original_owner"))
        new_owner = await db.users.create(make_user("new_owner"))
        workspace = await db.workspaces.create(make_workspace())

        await db.user_workspaces.create(
            make_user_workspace(original_owner.id, workspace.id, WorkspaceRole.OWNER)
        )
        await db.user_workspaces.create(
            make_user_workspace(new_owner.id, workspace.id, WorkspaceRole.ADMIN)
        )

        old_membership, new_membership = await db.workspaces.transfer_ownership(
            workspace.id, original_owner.id, new_owner.id
        )

        assert old_membership is not None
        assert new_membership is not None
        assert old_membership.role == WorkspaceRole.ADMIN
        assert new_membership.role == WorkspaceRole.OWNER

    async def test_remove_member_from_workspace(self, db: Database):
        """Test removing a member from a workspace."""
        owner = await db.users.create(make_user("owner"))
        member = await db.users.create(make_user("member"))
        workspace = await db.workspaces.create(make_workspace())

        await db.user_workspaces.create(
            make_user_workspace(owner.id, workspace.id, WorkspaceRole.OWNER)
        )
        _ = await db.user_workspaces.create(
            make_user_workspace(member.id, workspace.id, WorkspaceRole.MEMBER)
        )

        await db.user_workspaces.update_status(
            user_id=member.id,
            workspace_id=workspace.id,
            status=UserWorkspaceStatus.SUSPENDED,
        )

        updated = await db.user_workspaces.get_by_user_and_workspace(
            user_id=member.id, workspace_id=workspace.id
        )
        assert updated is not None
        assert updated.status == UserWorkspaceStatus.SUSPENDED


@requires_db
class TestUsageTrackingWorkflow:
    """Test usage tracking workflow."""

    async def test_usage_collection_and_billing(self, db: Database):
        """Test collecting and billing usage records."""
        user = await db.users.create(make_user())
        workspace = await db.workspaces.create(make_workspace())
        await db.user_workspaces.create(make_user_workspace(user.id, workspace.id))

        today = datetime.now(timezone.utc).date()

        # Create and increment daily record
        record = await db.usage.get_or_create_daily_record(
            workspace_id=workspace.id,
            usage_date=today,
        )

        for i in range(3):
            await db.usage.increment_usage(
                record_id=record.id,
                cpu_core_seconds=100.0 * (i + 1),
                memory_gb_seconds=0.0,
                build_minutes=0.0,
            )

        records = await db.usage.get_workspace_daily_usage(
            workspace_id=workspace.id,
            start_date=today,
            end_date=today,
        )

        assert len(records) == 1
        assert records[0].cpu_core_seconds == 600.0  # 100 + 200 + 300
        assert records[0].intervals_collected == 3

    async def test_usage_billing_workflow(self, db: Database):
        """Test complete usage billing workflow."""
        user = await db.users.create(make_user())
        workspace = await db.workspaces.create(make_workspace())
        await db.user_workspaces.create(make_user_workspace(user.id, workspace.id))

        today = datetime.now(timezone.utc).date()
        record = await db.usage.get_or_create_daily_record(
            workspace_id=workspace.id,
            usage_date=today,
        )

        await db.usage.increment_usage(
            record_id=record.id,
            cpu_core_seconds=500.0,
            memory_gb_seconds=0.0,
            build_minutes=0.0,
        )

        # Mark as billed
        await db.usage.mark_as_billed(record.id, "billing-123")

        # Verify billed status
        records = await db.usage.get_workspace_daily_usage(
            workspace_id=workspace.id,
            start_date=today,
            end_date=today,
        )
        assert records[0].status == DailyUsageStatus.BILLED
        assert records[0].billing_id == "billing-123"


@requires_db
class TestWorkspaceCleanupWorkflow:
    """Test workspace cleanup workflow."""

    async def test_workspace_deletion_cascade(self, db: Database):
        """Test that workspace deletion properly handles related entities."""
        user = await db.users.create(make_user())
        workspace = await db.workspaces.create(make_workspace())
        await db.user_workspaces.create(make_user_workspace(user.id, workspace.id))

        deployment = await db.compose_deployments.create(make_deployment(workspace.id))

        await db.secrets.create(make_secret(deployment.id))

        # Create daily usage record
        today = datetime.now(timezone.utc).date()
        await db.usage.get_or_create_daily_record(workspace.id, today)

        await db.workspaces.update_status(workspace.id, WorkspaceStatus.DELETED)

        deleted_ws = await db.workspaces.get_by_id(workspace.id)
        assert deleted_ws is None

        deleted_ws_with_flag = await db.workspaces.get_by_id(
            workspace.id, include_deleted=True
        )
        assert deleted_ws_with_flag is not None
        assert deleted_ws_with_flag.status == WorkspaceStatus.DELETED


@requires_db
class TestMultiTenantWorkflow:
    """Test multi-tenant scenarios."""

    async def test_user_with_multiple_workspaces(self, db: Database):
        """Test user managing multiple workspaces."""
        user = await db.users.create(make_user())

        personal = await db.workspaces.create(
            make_workspace(name="Personal", is_personal=True)
        )
        team_a = await db.workspaces.create(make_workspace(name="Team A"))
        team_b = await db.workspaces.create(make_workspace(name="Team B"))

        await db.user_workspaces.create(
            make_user_workspace(user.id, personal.id, WorkspaceRole.OWNER)
        )
        await db.user_workspaces.create(
            make_user_workspace(user.id, team_a.id, WorkspaceRole.OWNER)
        )
        await db.user_workspaces.create(
            make_user_workspace(user.id, team_b.id, WorkspaceRole.MEMBER)
        )

        memberships = await db.user_workspaces.get_user_memberships(user.id)

        assert len(memberships) == 3

        owned = [m for m in memberships if m.role == WorkspaceRole.OWNER]
        member = [m for m in memberships if m.role == WorkspaceRole.MEMBER]

        assert len(owned) == 2
        assert len(member) == 1

    async def test_workspace_with_multiple_deployments(self, db: Database):
        """Test workspace with multiple deployments."""
        user = await db.users.create(make_user())
        workspace = await db.workspaces.create(make_workspace())
        await db.user_workspaces.create(make_user_workspace(user.id, workspace.id))

        apps = ["frontend", "backend", "worker", "scheduler"]

        for app in apps:
            await db.compose_deployments.create(make_deployment(workspace.id, name=app))

        count = await db.compose_deployments.get_deployment_count(workspace.id)
        assert count == 4

        active_map = await db.compose_deployments.get_active_deployments_for_workspace(
            workspace.id
        )
        assert len(active_map) == 4
        for app in apps:
            assert app in active_map
