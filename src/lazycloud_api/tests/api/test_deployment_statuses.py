"""Tests for deployment status stream API routes."""

from unittest.mock import patch

import pytest
from httpx import AsyncClient

from lazycloud_api.database import Database
from lazycloud_api.database.users import UserPydantic
from lazycloud_api.tests.fixtures.database import (
    make_deployment,
    make_user_workspace,
    make_workspace,
    requires_db,
)
from shared.models.workspaces import WorkspaceRole

pytestmark = [pytest.mark.asyncio, requires_db]


class TestStreamDeploymentStatus:
    """Tests for GET /v1/deployments/{id}/status/stream."""

    async def test_stream_deployment_status(
        self, client: AsyncClient, api_db: Database, api_user: UserPydantic
    ):
        """Stream deployment status returns SSE stream."""
        workspace = await api_db.workspaces.create(make_workspace())
        await api_db.user_workspaces.create(
            make_user_workspace(api_user.id, workspace.id, WorkspaceRole.OWNER)
        )

        deployment = await api_db.compose_deployments.create(
            make_deployment(workspace.id, name="test-deploy")
        )

        # Mock the SSE stream to return immediately without blocking
        async def mock_stream_generator():
            yield {"event": "status", "data": '{"status": "test"}'}

        def mock_stream(*args, **kwargs):
            return mock_stream_generator()

        with patch(
            "lazycloud_api.api.v1.deployments.statuses.create_sse_stream_with_subscription",
            side_effect=mock_stream,
        ):
            response = await client.get(
                f"/v1/deployments/{deployment.id}/status/stream"
            )

        assert response.status_code == 200
        assert "text/event-stream" in response.headers["content-type"]
