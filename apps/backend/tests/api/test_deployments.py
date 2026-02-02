"""Tests for deployment API routes."""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from backend.database import Database
from backend.database.models import UserInDb, WorkspaceRole
from httpx import AsyncClient
from models.deployments import DeploymentStates
from models.statuses import DeploymentStatus, StatusPhase, TaskStatus

from tests.fixtures.database import (
    make_deployment,
    make_user_workspace,
    make_workspace,
    requires_db,
)

pytestmark = [pytest.mark.asyncio, requires_db]


@pytest.fixture
def mock_cluster_available():
    """Mock cluster availability and Polar service for deployment creation tests."""
    mock_polar = MagicMock()
    mock_polar.enabled = False

    with (
        patch(
            "backend.api.v1.deployments.root.is_cluster_available",
            return_value=True,
        ),
        # Patch in all locations where get_polar_service is imported
        patch(
            "backend.api.dependencies.get_polar_service",
            return_value=mock_polar,
        ),
        patch(
            "backend.services.get_polar_service",
            return_value=mock_polar,
        ),
    ):
        yield


SIMPLE_COMPOSE = """
version: '3.8'
services:
  web:
    image: nginx
    ports:
      - "80:80"
"""


class TestListDeployments:
    """Tests for GET /v1/deployments."""

    async def test_list_deployments_requires_workspace_id(self, client: AsyncClient):
        """Listing deployments requires workspace_id parameter."""
        response = await client.get("/v1/deployments")
        assert response.status_code == 422

    async def test_list_deployments_empty(
        self, client: AsyncClient, api_db: Database, api_user: UserInDb
    ):
        """Empty workspace returns empty deployment list."""
        workspace = await api_db.workspaces.create(make_workspace())
        await api_db.user_workspaces.create(
            make_user_workspace(api_user.id, workspace.id, WorkspaceRole.OWNER)
        )

        response = await client.get(f"/v1/deployments?workspace_id={workspace.id}")

        assert response.status_code == 200
        data = response.json()
        assert data["deployments"] == []
        assert data["total"] == 0

    async def test_list_deployments_returns_user_deployments(
        self, client: AsyncClient, api_db: Database, api_user: UserInDb
    ):
        """User sees deployments from their workspaces."""
        workspace = await api_db.workspaces.create(make_workspace())
        await api_db.user_workspaces.create(
            make_user_workspace(api_user.id, workspace.id, WorkspaceRole.OWNER)
        )

        await api_db.compose_deployments.create(
            make_deployment(workspace.id, name="deploy-1")
        )
        await api_db.compose_deployments.create(
            make_deployment(workspace.id, name="deploy-2")
        )

        response = await client.get(f"/v1/deployments?workspace_id={workspace.id}")

        assert response.status_code == 200
        data = response.json()
        assert len(data["deployments"]) == 2
        names = {d["name"] for d in data["deployments"]}
        assert names == {"deploy-1", "deploy-2"}

    async def test_list_deployments_pagination(
        self, client: AsyncClient, api_db: Database, api_user: UserInDb
    ):
        """Deployment list supports pagination."""
        workspace = await api_db.workspaces.create(make_workspace())
        await api_db.user_workspaces.create(
            make_user_workspace(api_user.id, workspace.id, WorkspaceRole.OWNER)
        )

        for i in range(5):
            await api_db.compose_deployments.create(
                make_deployment(workspace.id, name=f"deploy-{i}")
            )

        response = await client.get(
            f"/v1/deployments?workspace_id={workspace.id}&limit=2"
        )

        assert response.status_code == 200
        data = response.json()
        assert len(data["deployments"]) == 2
        assert data["has_more"] is True
        assert data["cursor"] is not None

    async def test_list_deployments_filters_by_status(
        self, client: AsyncClient, api_db: Database, api_user: UserInDb
    ):
        """Deployment list can filter by status."""
        workspace = await api_db.workspaces.create(make_workspace())
        await api_db.user_workspaces.create(
            make_user_workspace(api_user.id, workspace.id, WorkspaceRole.OWNER)
        )

        await api_db.compose_deployments.create(
            make_deployment(
                workspace.id, name="deployed", state=DeploymentStates.DEPLOYED
            )
        )
        await api_db.compose_deployments.create(
            make_deployment(
                workspace.id, name="pending", state=DeploymentStates.PENDING
            )
        )

        response = await client.get(
            f"/v1/deployments?workspace_id={workspace.id}&status={DeploymentStates.DEPLOYED.value}"
        )

        assert response.status_code == 200
        data = response.json()
        assert "deployments" in data
        # Verify filtering works: only DEPLOYED should be returned, not PENDING
        assert len(data["deployments"]) == 1
        assert data["deployments"][0]["name"] == "deployed"
        assert data["deployments"][0]["state"] == DeploymentStates.DEPLOYED.value

    async def test_list_deployments_requires_workspace_access(
        self, client: AsyncClient, api_db: Database
    ):
        """User cannot list deployments from workspaces they don't belong to."""
        other_workspace = await api_db.workspaces.create(make_workspace())
        await api_db.compose_deployments.create(
            make_deployment(other_workspace.id, name="other-deploy")
        )

        response = await client.get(
            f"/v1/deployments?workspace_id={other_workspace.id}"
        )

        assert response.status_code == 404


class TestCreateDeployment:
    """Tests for POST /v1/deployments."""

    async def test_create_deployment_success(
        self,
        client: AsyncClient,
        api_db: Database,
        api_user: UserInDb,
        mock_cluster_available,
    ):
        """Successfully create a new deployment."""
        workspace = await api_db.workspaces.create(make_workspace())
        await api_db.user_workspaces.create(
            make_user_workspace(api_user.id, workspace.id, WorkspaceRole.OWNER)
        )

        response = await client.post(
            "/v1/deployments",
            json={
                "workspace_id": str(workspace.id),
                "name": "test-deploy",
                "compose_yaml": SIMPLE_COMPOSE,
            },
        )

        assert response.status_code == 200
        data = response.json()
        assert data["name"] == "test-deploy"
        assert data["state"] == DeploymentStates.PENDING.value
        assert data["workspace_id"] == str(workspace.id)

    async def test_create_deployment_requires_admin_or_owner(
        self,
        client: AsyncClient,
        api_db: Database,
        api_user: UserInDb,
        mock_cluster_available,
    ):
        """Only admins and owners can create deployments."""
        workspace = await api_db.workspaces.create(make_workspace())
        await api_db.user_workspaces.create(
            make_user_workspace(api_user.id, workspace.id, WorkspaceRole.MEMBER)
        )

        response = await client.post(
            "/v1/deployments",
            json={
                "workspace_id": str(workspace.id),
                "name": "test-deploy",
                "compose_yaml": SIMPLE_COMPOSE,
            },
        )

        assert response.status_code == 403

    async def test_create_deployment_invalid_compose(
        self,
        client: AsyncClient,
        api_db: Database,
        api_user: UserInDb,
        mock_cluster_available,
    ):
        """Invalid compose YAML is rejected."""
        workspace = await api_db.workspaces.create(make_workspace())
        await api_db.user_workspaces.create(
            make_user_workspace(api_user.id, workspace.id, WorkspaceRole.OWNER)
        )

        response = await client.post(
            "/v1/deployments",
            json={
                "workspace_id": str(workspace.id),
                "name": "test-deploy",
                "compose_yaml": "invalid: yaml: content",
            },
        )

        assert response.status_code == 400

    async def test_create_deployment_duplicate_name(
        self,
        client: AsyncClient,
        api_db: Database,
        api_user: UserInDb,
        mock_cluster_available,
    ):
        """Creating deployment with duplicate name updates existing."""
        workspace = await api_db.workspaces.create(make_workspace())
        await api_db.user_workspaces.create(
            make_user_workspace(api_user.id, workspace.id, WorkspaceRole.OWNER)
        )

        existing = await api_db.compose_deployments.create(
            make_deployment(workspace.id, name="existing")
        )

        response = await client.post(
            "/v1/deployments",
            json={
                "workspace_id": str(workspace.id),
                "name": "existing",
                "compose_yaml": SIMPLE_COMPOSE,
            },
        )

        assert response.status_code == 200
        data = response.json()
        assert data["id"] == str(existing.id)
        assert data["name"] == "existing"


class TestGetDeploymentStatus:
    """Tests for GET /v1/deployments/{id}/status."""

    async def test_get_deployment_status(
        self, client: AsyncClient, api_db: Database, api_user: UserInDb
    ):
        """Get deployment status returns status information."""

        workspace = await api_db.workspaces.create(make_workspace())
        await api_db.user_workspaces.create(
            make_user_workspace(api_user.id, workspace.id, WorkspaceRole.OWNER)
        )

        deployment = await api_db.compose_deployments.create(
            make_deployment(workspace.id, name="test-deploy", with_helm_values=True)
        )

        # Create a proper DeploymentStatus model for the mock
        mock_status = DeploymentStatus(
            deployment_id=str(deployment.id),
            deployment_name="test-deploy",
            namespace=deployment.namespace,
            status=StatusPhase.RUNNING,
            ready=True,
            last_checked=datetime.now(timezone.utc),
            total_services=1,
            ready_services=1,
            total_replicas=1,
            ready_replicas=1,
            services=[],
            volumes=[],
        )

        with patch(
            "backend.api.v1.deployments.root.StatusWatcher"
        ) as mock_watcher_class:
            mock_watcher = AsyncMock()
            mock_watcher.get_deployment_status = AsyncMock(return_value=mock_status)
            mock_watcher_class.return_value = mock_watcher

            response = await client.get(f"/v1/deployments/{deployment.id}/status")

        assert response.status_code == 200
        data = response.json()
        assert "status" in data
        assert data["status"]["status"] == StatusPhase.RUNNING.value

    async def test_get_deployment_status_not_found(self, client: AsyncClient):
        """Getting status for non-existent deployment returns 404."""
        fake_id = "00000000-0000-0000-0000-000000000000"
        response = await client.get(f"/v1/deployments/{fake_id}/status")
        assert response.status_code == 404


class TestDeployDeployment:
    """Tests for POST /v1/deployments/{id}/deploy."""

    async def test_deploy_deployment_triggers_task(
        self,
        client: AsyncClient,
        api_db: Database,
        api_user: UserInDb,
        mock_saq_tasks,
    ):
        """Deploying a deployment triggers SAQ job."""
        workspace = await api_db.workspaces.create(make_workspace())
        await api_db.user_workspaces.create(
            make_user_workspace(api_user.id, workspace.id, WorkspaceRole.OWNER)
        )

        deployment_data = make_deployment(workspace.id, name="test-deploy")
        deployment_data.compose_yaml = SIMPLE_COMPOSE
        deployment_data.pending_compose_yaml = ""
        deployment = await api_db.compose_deployments.create(deployment_data)

        # Mock Polar service as disabled to skip subscription checks
        mock_polar = MagicMock()
        mock_polar.enabled = False

        with patch(
            "backend.api.dependencies.get_polar_service",
            return_value=mock_polar,
        ):
            response = await client.post(
                f"/v1/deployments/{deployment.id}/deploy",
                json={"secrets": False, "service_names": None},
            )

        assert response.status_code == 200
        data = response.json()
        assert "task_id" in data
        assert data["status"] == TaskStatus.PENDING.value
        mock_saq_tasks["deploy"].assert_called_once()

    async def test_deploy_deployment_requires_admin(
        self, client: AsyncClient, api_db: Database, api_user: UserInDb
    ):
        """Only admins and owners can deploy."""
        workspace = await api_db.workspaces.create(make_workspace())
        await api_db.user_workspaces.create(
            make_user_workspace(api_user.id, workspace.id, WorkspaceRole.MEMBER)
        )

        deployment = await api_db.compose_deployments.create(
            make_deployment(workspace.id, name="test-deploy")
        )

        response = await client.post(
            f"/v1/deployments/{deployment.id}/deploy",
            json={"secrets": False},
        )

        assert response.status_code == 403


class TestDeleteDeployment:
    """Tests for DELETE /v1/deployments/{id}."""

    async def test_delete_deployment_triggers_task(
        self,
        client: AsyncClient,
        api_db: Database,
        api_user: UserInDb,
        mock_saq_tasks,
    ):
        """Deleting a deployment triggers destroy job."""
        workspace = await api_db.workspaces.create(make_workspace())
        await api_db.user_workspaces.create(
            make_user_workspace(api_user.id, workspace.id, WorkspaceRole.OWNER)
        )

        deployment = await api_db.compose_deployments.create(
            make_deployment(workspace.id, name="test-deploy")
        )

        response = await client.delete(f"/v1/deployments/{deployment.id}")

        assert response.status_code == 200
        data = response.json()
        assert "task_id" in data
        assert data["status"] == TaskStatus.PENDING.value
        mock_saq_tasks["destroy"].assert_called_once()

    async def test_delete_deployment_requires_admin(
        self, client: AsyncClient, api_db: Database, api_user: UserInDb
    ):
        """Only admins and owners can delete deployments."""
        workspace = await api_db.workspaces.create(make_workspace())
        await api_db.user_workspaces.create(
            make_user_workspace(api_user.id, workspace.id, WorkspaceRole.MEMBER)
        )

        deployment = await api_db.compose_deployments.create(
            make_deployment(workspace.id, name="test-deploy")
        )

        response = await client.delete(f"/v1/deployments/{deployment.id}")

        assert response.status_code == 403


class TestRollbackDeployment:
    """Tests for POST /v1/deployments/{id}/rollback."""

    async def test_rollback_deployment_triggers_task(
        self,
        client: AsyncClient,
        api_db: Database,
        api_user: UserInDb,
        mock_saq_tasks,
    ):
        """Rolling back a deployment triggers rollback job."""
        workspace = await api_db.workspaces.create(make_workspace())
        await api_db.user_workspaces.create(
            make_user_workspace(api_user.id, workspace.id, WorkspaceRole.OWNER)
        )

        deployment = await api_db.compose_deployments.create(
            make_deployment(workspace.id, name="test-deploy")
        )

        response = await client.post(
            f"/v1/deployments/{deployment.id}/rollback",
            json={"revision": 1},
        )

        assert response.status_code == 200
        data = response.json()
        assert "task_id" in data
        assert data["status"] == TaskStatus.PENDING.value
        mock_saq_tasks["rollback"].assert_called_once()


class TestGetDeploymentHistory:
    """Tests for GET /v1/deployments/{id}/history."""

    async def test_get_deployment_history(
        self, client: AsyncClient, api_db: Database, api_user: UserInDb
    ):
        """Get deployment history returns Helm revisions."""
        workspace = await api_db.workspaces.create(make_workspace())
        await api_db.user_workspaces.create(
            make_user_workspace(api_user.id, workspace.id, WorkspaceRole.OWNER)
        )

        deployment = await api_db.compose_deployments.create(
            make_deployment(workspace.id, name="test-deploy")
        )

        with patch("backend.api.v1.deployments.root.HelmManager") as mock_helm:
            mock_manager = MagicMock()
            mock_manager.get_history = AsyncMock(return_value=[])
            mock_helm.return_value = mock_manager

            response = await client.get(f"/v1/deployments/{deployment.id}/history")

        assert response.status_code == 200
        data = response.json()
        assert "revisions" in data
