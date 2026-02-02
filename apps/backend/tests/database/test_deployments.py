"""Tests for ComposeDeploymentService database operations."""

from datetime import datetime, timedelta, timezone

from backend.database import Database
from backend.database.models import WorkspaceRole
from models.deployments import DeploymentStates

from tests.fixtures.database import (
    make_deployment,
    make_user,
    make_user_workspace,
    make_workspace,
    requires_db,
)


@requires_db
class TestDeploymentServiceCRUD:
    """Test basic CRUD operations for ComposeDeploymentService."""

    async def test_create_deployment(self, db: Database):
        """Test creating a deployment."""
        workspace = await db.workspaces.create(make_workspace())
        deployment = make_deployment(workspace.id)
        created = await db.compose_deployments.create(deployment)

        assert created.id is not None
        assert created.name == deployment.name
        assert created.namespace == deployment.namespace
        assert created.workspace_id == workspace.id
        assert created.state == DeploymentStates.DEPLOYED
        assert created.deleted_at is None
        assert created.created_at is not None

    async def test_create_pending_deployment(self, db: Database):
        """Test creating a deployment in pending state."""
        workspace = await db.workspaces.create(make_workspace())
        deployment = make_deployment(workspace.id, state=DeploymentStates.PENDING)
        created = await db.compose_deployments.create(deployment)

        assert created.state == DeploymentStates.PENDING

    async def test_get_deployment_by_id(self, db: Database):
        """Test retrieving a deployment by ID."""
        workspace = await db.workspaces.create(make_workspace())
        deployment = make_deployment(workspace.id)
        created = await db.compose_deployments.create(deployment)

        retrieved = await db.compose_deployments.get_by_id(created.id)

        assert retrieved is not None
        assert retrieved.id == created.id
        assert retrieved.name == created.name

    async def test_update_deployment(self, db: Database):
        """Test updating a deployment."""
        workspace = await db.workspaces.create(make_workspace())
        deployment = make_deployment(workspace.id)
        created = await db.compose_deployments.create(deployment)

        created.status_message = "Updated message"
        updated = await db.compose_deployments.update(created)

        assert updated is not None
        assert updated.status_message == "Updated message"

    async def test_delete_deployment(self, db: Database):
        """Test deleting a deployment."""
        workspace = await db.workspaces.create(make_workspace())
        deployment = make_deployment(workspace.id)
        created = await db.compose_deployments.create(deployment)

        await db.compose_deployments.delete(created.id)

        retrieved = await db.compose_deployments.get_by_id(created.id)
        assert retrieved is None


@requires_db
class TestDeploymentServiceQueries:
    """Test custom query methods for ComposeDeploymentService."""

    async def test_get_by_name(self, db: Database):
        """Test retrieving a deployment by name."""
        workspace = await db.workspaces.create(make_workspace())
        deployment = make_deployment(workspace.id, name="my-app")
        await db.compose_deployments.create(deployment)

        retrieved = await db.compose_deployments.get_by_name(workspace.id, "my-app")

        assert retrieved is not None
        assert retrieved.name == "my-app"

    async def test_get_by_name_not_found(self, db: Database):
        """Test get_by_name returns None for non-existent deployment."""
        workspace = await db.workspaces.create(make_workspace())

        result = await db.compose_deployments.get_by_name(workspace.id, "non-existent")
        assert result is None

    async def test_get_by_name_excludes_soft_deleted(self, db: Database):
        """Test get_by_name excludes soft-deleted deployments."""
        workspace = await db.workspaces.create(make_workspace())
        deployment = make_deployment(workspace.id, name="deleted-app")
        created = await db.compose_deployments.create(deployment)

        created.deleted_at = datetime.now(timezone.utc)
        await db.compose_deployments.update(created)

        result = await db.compose_deployments.get_by_name(workspace.id, "deleted-app")
        assert result is None

    async def test_find_by_namespace(self, db: Database):
        """Test finding deployment by namespace."""
        workspace = await db.workspaces.create(make_workspace())
        deployment = make_deployment(workspace.id)
        created = await db.compose_deployments.create(deployment)

        result = await db.compose_deployments.find_by_namespace(
            workspace.id, created.namespace
        )

        assert result is not None
        assert result.namespace == created.namespace

    async def test_find_by_status(self, db: Database):
        """Test finding deployments by status."""
        workspace = await db.workspaces.create(make_workspace())

        dep1 = make_deployment(
            workspace.id, name="dep1", state=DeploymentStates.DEPLOYED
        )
        dep2 = make_deployment(
            workspace.id, name="dep2", state=DeploymentStates.DEPLOYED
        )
        dep3 = make_deployment(
            workspace.id, name="dep3", state=DeploymentStates.PENDING
        )

        await db.compose_deployments.create(dep1)
        await db.compose_deployments.create(dep2)
        await db.compose_deployments.create(dep3)

        deployed = await db.compose_deployments.find_by_status(
            workspace.id, DeploymentStates.DEPLOYED
        )
        pending = await db.compose_deployments.find_by_status(
            workspace.id, DeploymentStates.PENDING
        )

        assert len(deployed) == 2
        assert len(pending) == 1


@requires_db
class TestDeploymentServiceStatus:
    """Test deployment status operations."""

    async def test_update_status(self, db: Database):
        """Test updating deployment status."""
        workspace = await db.workspaces.create(make_workspace())
        deployment = make_deployment(workspace.id, state=DeploymentStates.PENDING)
        created = await db.compose_deployments.create(deployment)

        updated = await db.compose_deployments.update_status(
            created.id, DeploymentStates.DEPLOYING, message="Deploying..."
        )

        assert updated is not None
        assert updated.state == DeploymentStates.DEPLOYING
        assert updated.status_message == "Deploying..."

    async def test_update_status_to_deployed(self, db: Database):
        """Test updating status to deployed."""
        workspace = await db.workspaces.create(make_workspace())
        deployment = make_deployment(workspace.id, state=DeploymentStates.DEPLOYING)
        created = await db.compose_deployments.create(deployment)

        updated = await db.compose_deployments.update_status(
            created.id, DeploymentStates.DEPLOYED
        )

        assert updated is not None
        assert updated.state == DeploymentStates.DEPLOYED

    async def test_update_status_non_existent_deployment(self, db: Database):
        """Test update_status returns None for non-existent deployment."""
        result = await db.compose_deployments.update_status(
            "00000000-0000-0000-0000-000000000000", DeploymentStates.DEPLOYED
        )
        assert result is None


@requires_db
class TestDeploymentServiceAccessControl:
    """Test deployment access control queries."""

    async def test_get_with_workspace_access(self, db: Database):
        """Test getting deployment with user's workspace role."""
        user = await db.users.create(make_user())
        workspace = await db.workspaces.create(make_workspace())
        await db.user_workspaces.create(
            make_user_workspace(user.id, workspace.id, WorkspaceRole.ADMIN)
        )
        deployment = make_deployment(workspace.id)
        created = await db.compose_deployments.create(deployment)

        result, role = await db.compose_deployments.get_with_workspace_access(
            created.id, user.id
        )

        assert result is not None
        assert result.id == created.id
        assert role == WorkspaceRole.ADMIN.value

    async def test_get_with_workspace_access_no_access(self, db: Database):
        """Test get_with_workspace_access returns None when user has no access."""
        user = await db.users.create(make_user())
        workspace = await db.workspaces.create(make_workspace())
        deployment = make_deployment(workspace.id)
        created = await db.compose_deployments.create(deployment)

        result, role = await db.compose_deployments.get_with_workspace_access(
            created.id, user.id
        )

        assert result is None
        assert role is None


@requires_db
class TestDeploymentServiceCounts:
    """Test deployment counting methods."""

    async def test_get_deployment_count(self, db: Database):
        """Test counting deployments for a workspace."""
        workspace = await db.workspaces.create(make_workspace())

        for i in range(3):
            dep = make_deployment(workspace.id, name=f"deployment-{i}")
            await db.compose_deployments.create(dep)

        count = await db.compose_deployments.get_deployment_count(workspace.id)
        assert count == 3

    async def test_get_deployment_count_excludes_soft_deleted(self, db: Database):
        """Test deployment count excludes soft-deleted deployments."""
        workspace = await db.workspaces.create(make_workspace())

        dep1 = make_deployment(workspace.id, name="active-1")
        dep2 = make_deployment(workspace.id, name="active-2")
        dep3 = make_deployment(workspace.id, name="deleted")

        await db.compose_deployments.create(dep1)
        await db.compose_deployments.create(dep2)
        deleted = await db.compose_deployments.create(dep3)

        deleted.deleted_at = datetime.now(timezone.utc)
        await db.compose_deployments.update(deleted)

        count = await db.compose_deployments.get_deployment_count(workspace.id)
        assert count == 2

    async def test_get_deployment_counts_by_workspace(self, db: Database):
        """Test getting deployment counts for multiple workspaces."""
        ws1 = await db.workspaces.create(make_workspace())
        ws2 = await db.workspaces.create(make_workspace())
        ws3 = await db.workspaces.create(make_workspace())

        for i in range(2):
            await db.compose_deployments.create(
                make_deployment(ws1.id, name=f"ws1-dep-{i}")
            )

        for i in range(3):
            await db.compose_deployments.create(
                make_deployment(ws2.id, name=f"ws2-dep-{i}")
            )

        counts = await db.compose_deployments.get_deployment_counts_by_workspace(
            [ws1.id, ws2.id, ws3.id]
        )

        assert counts.get(ws1.id) == 2
        assert counts.get(ws2.id) == 3
        assert ws3.id not in counts

    async def test_get_active_deployments_for_workspace(self, db: Database):
        """Test getting active deployments mapping for a workspace."""
        workspace = await db.workspaces.create(make_workspace())

        dep1 = make_deployment(workspace.id, name="app-1")
        dep2 = make_deployment(workspace.id, name="app-2")

        created1 = await db.compose_deployments.create(dep1)
        created2 = await db.compose_deployments.create(dep2)

        active_map = await db.compose_deployments.get_active_deployments_for_workspace(
            workspace.id
        )

        assert "app-1" in active_map
        assert "app-2" in active_map
        assert active_map["app-1"] == created1.id
        assert active_map["app-2"] == created2.id


@requires_db
class TestDeploymentServiceDateQueries:
    """Test date-based query methods."""

    async def test_find_active_during_date_range(self, db: Database):
        """Test finding deployments active during a date range."""
        workspace = await db.workspaces.create(make_workspace())

        dep = make_deployment(workspace.id, name="active-deployment")
        await db.compose_deployments.create(dep)

        now = datetime.now(timezone.utc)
        start = now - timedelta(hours=1)
        end = now + timedelta(hours=1)

        results = await db.compose_deployments.find_active_during_date_range(
            workspace.id, start, end
        )

        assert len(results) >= 1
        names = [d.name for d in results]
        assert "active-deployment" in names
