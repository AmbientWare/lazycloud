"""Tests for user API routes."""

import pytest
from backend.database import Database
from backend.database.models import UserPydantic, WorkspaceRole
from httpx import AsyncClient

from tests.fixtures.database import (
    make_deployment,
    make_user_workspace,
    make_workspace,
    requires_db,
)

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
    """Tests for GET /v1/users/features.

    Note: The features response has been simplified to a flat structure.
    Workspaces are unlimited, services/volumes/networks are unlimited.
    """

    async def test_returns_feature_limits(self, client: AsyncClient):
        """Features endpoint returns subscription limits."""
        response = await client.get("/v1/users/features")

        assert response.status_code == 200
        data = response.json()
        assert "deployment_limit" in data
        assert "deployment_count" in data
        assert "max_team_members" in data
        assert "max_cpu_per_service" in data
        assert "max_memory_per_service" in data
        assert "max_replicas_per_service" in data
        assert "custom_domains_enabled" in data

    async def test_features_response_structure(self, client: AsyncClient):
        """Features have correct types."""
        response = await client.get("/v1/users/features")

        assert response.status_code == 200
        data = response.json()

        assert isinstance(data["deployment_limit"], int)
        assert isinstance(data["deployment_count"], int)
        assert data["max_team_members"] is None or isinstance(
            data["max_team_members"], int
        )
        assert isinstance(data["max_cpu_per_service"], (int, float))
        assert isinstance(data["max_memory_per_service"], int)
        assert isinstance(data["max_replicas_per_service"], int)
        assert isinstance(data["custom_domains_enabled"], bool)

    async def test_deployment_count_reflects_total_deployments(
        self, client: AsyncClient, api_db: Database, api_user: UserPydantic
    ):
        """Deployment count matches total deployments across all workspaces."""
        # Create 2 workspaces with deployments
        ws1 = await api_db.workspaces.create(make_workspace(name="WS1"))
        ws2 = await api_db.workspaces.create(make_workspace(name="WS2"))
        await api_db.user_workspaces.create(
            make_user_workspace(api_user.id, ws1.id, WorkspaceRole.OWNER)
        )
        await api_db.user_workspaces.create(
            make_user_workspace(api_user.id, ws2.id, WorkspaceRole.OWNER)
        )

        # Create 2 deployments in ws1 and 1 in ws2
        await api_db.compose_deployments.create(make_deployment(ws1.id, name="d1"))
        await api_db.compose_deployments.create(make_deployment(ws1.id, name="d2"))
        await api_db.compose_deployments.create(make_deployment(ws2.id, name="d3"))

        response = await client.get("/v1/users/features")

        assert response.status_code == 200
        assert response.json()["deployment_count"] == 3
