"""Tests for deployment instances API routes."""

import pytest
from backend.database import Database
from backend.database.models import UserInDb, WorkspaceRole
from httpx import AsyncClient
from models.helm import HelmValues, ImageConfig, ServiceValues
from models.k8s import WorkloadType

from tests.fixtures.database import (
    make_deployment,
    make_user_workspace,
    make_workspace,
    requires_db,
)

pytestmark = [pytest.mark.asyncio, requires_db]


class TestDeleteInstance:
    """Tests for DELETE /v1/deployments/{id}/instances/{pod_name}."""

    async def test_delete_instance_triggers_task(
        self,
        client: AsyncClient,
        api_db: Database,
        api_user: UserInDb,
        mock_saq_tasks,
    ):
        """Deleting an instance triggers SAQ job."""
        workspace = await api_db.workspaces.create(make_workspace())
        await api_db.user_workspaces.create(
            make_user_workspace(api_user.id, workspace.id, WorkspaceRole.OWNER)
        )

        deployment_data = make_deployment(workspace.id, name="test-deploy")
        deployment_data.helm_values = HelmValues(
            services=[
                ServiceValues(
                    name="web",
                    image=ImageConfig(
                        repository="nginx",
                        tag="latest",
                        pullPolicy="IfNotPresent",
                    ),
                    resourceName="web",
                    replicas=1,
                    workloadType=WorkloadType.DEPLOYMENT,
                )
            ]
        )
        deployment = await api_db.compose_deployments.create(deployment_data)

        response = await client.delete(
            f"/v1/deployments/{deployment.id}/instances/web-123"
        )

        assert response.status_code == 200
        data = response.json()
        assert "task_id" in data
        assert data["status"] == "pending"
        mock_saq_tasks["delete_instance"].assert_called_once()
        call_kwargs = mock_saq_tasks["delete_instance"].call_args.kwargs
        assert call_kwargs["deployment_id"] == str(deployment.id)
        assert call_kwargs["pod_name"] == "web-123"

    async def test_delete_instance_requires_admin(
        self,
        client: AsyncClient,
        api_db: Database,
        api_user: UserInDb,
    ):
        """Only admins and owners can delete instances."""
        workspace = await api_db.workspaces.create(make_workspace())
        await api_db.user_workspaces.create(
            make_user_workspace(api_user.id, workspace.id, WorkspaceRole.MEMBER)
        )

        deployment = await api_db.compose_deployments.create(
            make_deployment(workspace.id, name="test-deploy")
        )

        response = await client.delete(
            f"/v1/deployments/{deployment.id}/instances/web-123"
        )

        assert response.status_code == 403
