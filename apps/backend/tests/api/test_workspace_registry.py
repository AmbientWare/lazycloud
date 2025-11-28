"""Tests for workspace registry API routes."""

from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest
from backend.database import Database
from backend.database.users import UserPydantic
from backend.services import get_ecr_auth_service
from httpx import AsyncClient
from models.registry import ECRCredentials
from models.workspaces import WorkspaceRole

from tests.api.conftest import get_test_app
from tests.fixtures.database import (
    make_user_workspace,
    make_workspace,
    requires_db,
)

pytestmark = [pytest.mark.asyncio, requires_db]


class TestGetUploadIntent:
    """Tests for POST /v1/workspaces/{id}/registry/upload-intent."""

    async def test_get_upload_intent(
        self, client: AsyncClient, api_db: Database, api_user: UserPydantic
    ):
        """Get upload intent for registry."""
        workspace = await api_db.workspaces.create(make_workspace())
        await api_db.user_workspaces.create(
            make_user_workspace(api_user.id, workspace.id, WorkspaceRole.OWNER)
        )

        # Override the dependency injection in the app
        app = get_test_app()
        mock_ecr_service = AsyncMock()
        credentials = ECRCredentials(
            registry_url="test-registry",
            username="test-user",
            password="test-password",
            repository="test-repo",
            expires_at=datetime.now(timezone.utc),
        )
        mock_ecr_service.get_upload_credentials = AsyncMock(return_value=credentials)

        # Override the dependency
        app.dependency_overrides[get_ecr_auth_service] = lambda: mock_ecr_service

        response = await client.post(
            f"/v1/workspaces/{workspace.id}/registry/upload-intent",
            json={
                "deployment_name": "test-deploy",
                "repo_name": "test-repo",
                "session_name": "test-session",
            },
        )

        assert response.status_code == 200
        data = response.json()
        assert data["registry_url"] == "test-registry"
        assert data["username"] == "test-user"
        assert data["password"] == "test-password"
        assert data["repository"] == "test-repo"


class TestCheckImagesExist:
    """Tests for POST /v1/workspaces/{id}/registry/images-exist."""

    async def test_check_images_exist(
        self, client: AsyncClient, api_db: Database, api_user: UserPydantic
    ):
        """Check if images exist in registry."""
        workspace = await api_db.workspaces.create(make_workspace())
        await api_db.user_workspaces.create(
            make_user_workspace(api_user.id, workspace.id, WorkspaceRole.OWNER)
        )

        # Override dependency in the app
        app = get_test_app()
        mock_ecr_service = AsyncMock()
        mock_ecr_service.check_images_exist = AsyncMock(
            return_value={"test-image:latest": True}
        )

        app.dependency_overrides[get_ecr_auth_service] = lambda: mock_ecr_service

        response = await client.post(
            f"/v1/workspaces/{workspace.id}/registry/images-exist",
            json={
                "deployment_name": "test-deploy",
                "image_names": ["test-image:latest"],
            },
        )

        assert response.status_code == 200
        data = response.json()
        assert "exists_map" in data
        assert data["exists_map"]["test-image:latest"] is True
