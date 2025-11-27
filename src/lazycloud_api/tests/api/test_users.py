"""Tests for user API routes."""

import pytest
from httpx import AsyncClient

from lazycloud_api.database import Database
from lazycloud_api.database.users import UserPydantic
from lazycloud_api.tests.fixtures.database import (
    make_user_workspace,
    make_workspace,
    requires_db,
)
from shared.models.workspaces import WorkspaceRole

pytestmark = [pytest.mark.asyncio, requires_db]


class TestCurrentUser:
    """Tests for GET /v1/users/current."""

    async def test_returns_current_user_id(
        self, client: AsyncClient, api_user: UserPydantic
    ):
        """Current user endpoint returns authenticated user's id."""
        response = await client.get("/v1/users/current")

        assert response.status_code == 200
        data = response.json()
        assert data["id"] == str(api_user.id)

    async def test_response_structure(self, client: AsyncClient):
        """CurrentUserResponse only includes id field."""
        response = await client.get("/v1/users/current")

        assert response.status_code == 200
        data = response.json()
        assert "id" in data


class TestUserFeatures:
    """Tests for GET /v1/users/features."""

    async def test_returns_feature_limits(self, client: AsyncClient):
        """Features endpoint returns subscription limits."""
        response = await client.get("/v1/users/features")

        assert response.status_code == 200
        data = response.json()
        assert "workspace" in data
        assert "deployment" in data
        assert "domain_limit" in data

    async def test_workspace_features_structure(self, client: AsyncClient):
        """Workspace features have correct structure."""
        response = await client.get("/v1/users/features")

        assert response.status_code == 200
        ws = response.json()["workspace"]
        assert "limit" in ws
        assert "deployment_limit" in ws
        assert "current_count" in ws

    async def test_deployment_features_structure(self, client: AsyncClient):
        """Deployment features have correct structure."""
        response = await client.get("/v1/users/features")

        assert response.status_code == 200
        dep = response.json()["deployment"]
        assert "service_limit" in dep
        assert "volume_limit" in dep
        assert "network_limit" in dep

    async def test_workspace_count_reflects_user_workspaces(
        self, client: AsyncClient, api_db: Database, api_user: UserPydantic
    ):
        """Workspace count matches actual user workspace count."""
        # Create 2 workspaces
        ws1 = await api_db.workspaces.create(make_workspace(name="WS1"))
        ws2 = await api_db.workspaces.create(make_workspace(name="WS2"))
        await api_db.user_workspaces.create(
            make_user_workspace(api_user.id, ws1.id, WorkspaceRole.OWNER)
        )
        await api_db.user_workspaces.create(
            make_user_workspace(api_user.id, ws2.id, WorkspaceRole.MEMBER)
        )

        response = await client.get("/v1/users/features")

        assert response.status_code == 200
        assert response.json()["workspace"]["current_count"] == 2
