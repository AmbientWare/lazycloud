"""Tests for workspace builds API routes."""

from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest
from httpx import AsyncClient

from lazycloud_api.database import Database
from lazycloud_api.database.users import UserPydantic
from lazycloud_api.services import get_depot_service, get_ecr_auth_service
from lazycloud_api.tests.api.conftest import get_test_app
from lazycloud_api.tests.fixtures.database import (
    make_deployment,
    make_user_workspace,
    make_workspace,
    requires_db,
)
from shared.models.depot import DepotBuildCredentials
from shared.models.workspaces import WorkspaceRole

pytestmark = [pytest.mark.asyncio, requires_db]


class TestGetDepotToken:
    """Tests for POST /v1/workspaces/{id}/builds/token."""

    async def test_get_depot_token(
        self, client: AsyncClient, api_db: Database, api_user: UserPydantic
    ):
        """Get Depot build token."""
        workspace = await api_db.workspaces.create(make_workspace())
        await api_db.user_workspaces.create(
            make_user_workspace(api_user.id, workspace.id, WorkspaceRole.OWNER)
        )

        await api_db.compose_deployments.create(
            make_deployment(workspace.id, name="test-deploy")
        )

        # Override dependencies in the app
        app = get_test_app()

        mock_depot_service = AsyncMock()
        mock_depot_service.is_configured = True
        mock_depot_service.get_build_token = AsyncMock(
            return_value=DepotBuildCredentials(
                project_id="test-project",
                token="test-token",
                expires_at=datetime.now(timezone.utc),
                registry_url="test-registry",
            )
        )

        mock_ecr_service = AsyncMock()
        mock_ecr_service.get_registry_url = lambda: "test-registry"

        app.dependency_overrides[get_depot_service] = lambda: mock_depot_service
        app.dependency_overrides[get_ecr_auth_service] = lambda: mock_ecr_service

        response = await client.post(
            f"/v1/workspaces/{workspace.id}/builds/token?deployment_name=test-deploy"
        )

        assert response.status_code == 200
        data = response.json()
        assert data["token"] == "test-token"
        assert data["project_id"] == "test-project"
        assert data["registry_url"] == "test-registry"
