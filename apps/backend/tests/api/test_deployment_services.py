"""Tests for deployment services API routes."""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest
from backend.database import Database
from backend.database.users import UserPydantic
from httpx import AsyncClient
from models.k8s import WorkloadType
from models.statuses import KubernetesPhase, ServiceStatus
from models.workspaces import WorkspaceRole

from tests.fixtures.database import (
    make_deployment,
    make_user_workspace,
    make_workspace,
    requires_db,
)

pytestmark = [pytest.mark.asyncio, requires_db]


class TestListServices:
    """Tests for GET /v1/deployments/{id}/services."""

    async def test_list_services(
        self, client: AsyncClient, api_db: Database, api_user: UserPydantic
    ):
        """List services for a deployment."""
        workspace = await api_db.workspaces.create(make_workspace())
        await api_db.user_workspaces.create(
            make_user_workspace(api_user.id, workspace.id, WorkspaceRole.OWNER)
        )

        deployment = await api_db.compose_deployments.create(
            make_deployment(workspace.id, name="test-deploy")
        )

        mock_status = ServiceStatus(
            name="web",
            image="nginx:latest",
            workload_type=WorkloadType.DEPLOYMENT,
            status=KubernetesPhase.RUNNING,
            replicas=1,
            ready_replicas=1,
            last_checked=datetime.now(timezone.utc),
        )

        with patch(
            "backend.api.v1.deployments.services.StatusWatcher"
        ) as mock_watcher_class:
            mock_watcher = AsyncMock()
            mock_watcher.get_service_statuses_for_deployment = AsyncMock(
                return_value=[mock_status]
            )
            mock_watcher_class.return_value = mock_watcher

            response = await client.get(f"/v1/deployments/{deployment.id}/services")

        assert response.status_code == 200
        services = response.json()
        assert len(services) == 1
        service = services[0]["service"]
        assert service["name"] == "web"
        assert service["image"] == "nginx:latest"
        assert service["status"] == KubernetesPhase.RUNNING.value


class TestRestartAllServices:
    """Tests for POST /v1/deployments/{id}/services/restart."""

    async def test_restart_all_services_triggers_task(
        self,
        client: AsyncClient,
        api_db: Database,
        api_user: UserPydantic,
        mock_saq_tasks,
    ):
        """Restarting all services triggers SAQ job."""
        workspace = await api_db.workspaces.create(make_workspace())
        await api_db.user_workspaces.create(
            make_user_workspace(api_user.id, workspace.id, WorkspaceRole.OWNER)
        )

        deployment = await api_db.compose_deployments.create(
            make_deployment(workspace.id, name="test-deploy")
        )

        response = await client.post(
            f"/v1/deployments/{deployment.id}/services/restart"
        )

        assert response.status_code == 200
        data = response.json()
        assert "task_id" in data
        assert data["status"] == "pending"
        mock_saq_tasks["restart_all"].assert_called_once()
        call_kwargs = mock_saq_tasks["restart_all"].call_args.kwargs
        assert call_kwargs["deployment_id"] == str(deployment.id)


class TestRestartService:
    """Tests for POST /v1/deployments/{id}/services/{name}/restart."""

    async def test_restart_service_triggers_task(
        self,
        client: AsyncClient,
        api_db: Database,
        api_user: UserPydantic,
        mock_saq_tasks,
    ):
        """Restarting a service triggers SAQ job."""
        workspace = await api_db.workspaces.create(make_workspace())
        await api_db.user_workspaces.create(
            make_user_workspace(api_user.id, workspace.id, WorkspaceRole.OWNER)
        )

        deployment = await api_db.compose_deployments.create(
            make_deployment(workspace.id, name="test-deploy")
        )

        response = await client.post(
            f"/v1/deployments/{deployment.id}/services/web/restart"
        )

        assert response.status_code == 200
        data = response.json()
        assert "task_id" in data
        assert data["status"] == "pending"
        mock_saq_tasks["restart_service"].assert_called_once()
        call_kwargs = mock_saq_tasks["restart_service"].call_args.kwargs
        assert call_kwargs["deployment_id"] == str(deployment.id)
        assert call_kwargs["service_name"] == "web"
