"""Tests for extended workspace API routes."""

import pytest
from backend.database import Database
from backend.database.models import User, WorkspaceRole
from httpx import AsyncClient

from tests.fixtures.database import (
    make_deployment,
    make_user_workspace,
    make_workspace,
    requires_db,
)

pytestmark = [pytest.mark.asyncio, requires_db]


class TestGetWorkspaceWithDeployments:
    """Tests for GET /v1/workspaces/{id}/with-deployments."""

    async def test_get_workspace_with_deployments(
        self, client: AsyncClient, api_db: Database, api_user: User
    ):
        """Get workspace includes deployment list."""
        workspace = await api_db.workspaces.create(make_workspace())
        await api_db.user_workspaces.create(
            make_user_workspace(api_user.id, workspace.id, WorkspaceRole.OWNER)
        )

        await api_db.compose_deployments.create(
            make_deployment(workspace.id, name="deploy-1")
        )

        response = await client.get(f"/v1/workspaces/{workspace.id}/with-deployments")

        assert response.status_code == 200
        data = response.json()
        assert data["id"] == str(workspace.id)
        assert data["name"] == workspace.name
        assert len(data["deployments"]) == 1
        assert data["deployments"][0]["name"] == "deploy-1"


class TestGetAggregatedUsage:
    """Tests for GET /v1/workspaces/usage/all."""

    async def test_get_aggregated_usage(
        self, client: AsyncClient, api_db: Database, api_user: User
    ):
        """Get aggregated usage across workspaces."""
        workspace = await api_db.workspaces.create(make_workspace())
        await api_db.user_workspaces.create(
            make_user_workspace(api_user.id, workspace.id, WorkspaceRole.OWNER)
        )

        response = await client.get("/v1/workspaces/usage/all")

        assert response.status_code == 200
        data = response.json()
        assert "workspaces" in data
        assert "period" in data
        assert "usage" in data
        assert "start" in data["period"]
        assert "end" in data["period"]
        # Verify usage structure has expected metrics
        usage = data["usage"]
        assert "cpu_core_hours" in usage
        assert "memory_gb_hours" in usage
        assert isinstance(usage["cpu_core_hours"], (int, float))
        assert isinstance(usage["memory_gb_hours"], (int, float))


class TestGetDailyUsage:
    """Tests for GET /v1/workspaces/usage/all/daily."""

    async def test_get_daily_usage(
        self, client: AsyncClient, api_db: Database, api_user: User
    ):
        """Get daily aggregated usage."""
        workspace = await api_db.workspaces.create(make_workspace())
        await api_db.user_workspaces.create(
            make_user_workspace(api_user.id, workspace.id, WorkspaceRole.OWNER)
        )

        response = await client.get("/v1/workspaces/usage/all/daily")

        assert response.status_code == 200
        data = response.json()
        assert "daily_usage" in data
        assert "period" in data
        assert isinstance(data["daily_usage"], list)
