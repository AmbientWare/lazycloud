"""Tests for diff API routes."""

from unittest.mock import patch

import pytest
from backend.database import Database
from backend.database.users import UserPydantic
from httpx import AsyncClient
from models.workspaces import WorkspaceRole

from tests.fixtures.database import (
    make_deployment,
    make_user_workspace,
    make_workspace,
    requires_db,
)

pytestmark = [pytest.mark.asyncio, requires_db]

SIMPLE_COMPOSE = """
version: '3.8'
services:
  web:
    image: nginx
"""


class TestGetDeploymentDiff:
    """Tests for POST /v1/diff."""

    async def test_diff_new_deployment(
        self, client: AsyncClient, api_db: Database, api_user: UserPydantic
    ):
        """Get diff for new deployment."""
        workspace = await api_db.workspaces.create(make_workspace())
        await api_db.user_workspaces.create(
            make_user_workspace(api_user.id, workspace.id, WorkspaceRole.OWNER)
        )

        with patch("backend.api.v1.diff.get_namespace_pvcs", return_value=[]):
            response = await client.post(
                "/v1/diff",
                json={
                    "diff_type": "new",
                    "workspace_id": str(workspace.id),
                    "deployment_name": "new-deploy",
                    "compose_yaml": SIMPLE_COMPOSE,
                    "env_keys": [],
                },
            )

        assert response.status_code == 200
        data = response.json()
        assert data["deployment_id"] == "new"
        assert "diff" in data

    async def test_diff_existing_deployment(
        self, client: AsyncClient, api_db: Database, api_user: UserPydantic
    ):
        """Get diff for existing deployment."""
        workspace = await api_db.workspaces.create(make_workspace())
        await api_db.user_workspaces.create(
            make_user_workspace(api_user.id, workspace.id, WorkspaceRole.OWNER)
        )

        deployment_data = make_deployment(workspace.id, name="existing")
        deployment_data.compose_yaml = SIMPLE_COMPOSE
        deployment = await api_db.compose_deployments.create(deployment_data)

        with patch("backend.api.v1.diff.get_namespace_pvcs", return_value=[]):
            response = await client.post(
                "/v1/diff",
                json={
                    "diff_type": "existing",
                    "workspace_id": str(workspace.id),
                    "deployment_name": "existing",
                    "compose_yaml": SIMPLE_COMPOSE,
                    "env_keys": [],
                },
            )

        assert response.status_code == 200
        data = response.json()
        assert data["deployment_id"] == str(deployment.id)
