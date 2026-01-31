"""Tests for deployment secrets API routes."""

import json

import pytest
from backend.database import Database
from backend.database.models import User, WorkspaceRole
from httpx import AsyncClient
from models.secrets import SecretSource

from tests.fixtures.database import (
    make_deployment,
    make_secret,
    make_user_workspace,
    make_workspace,
    requires_db,
)

pytestmark = [pytest.mark.asyncio, requires_db]


class TestListSecrets:
    """Tests for GET /v1/deployments/{id}/secrets."""

    async def test_list_secrets_hides_values_by_default(
        self, client: AsyncClient, api_db: Database, api_user: User
    ):
        """Secret values are hidden by default."""
        workspace = await api_db.workspaces.create(make_workspace())
        await api_db.user_workspaces.create(
            make_user_workspace(api_user.id, workspace.id, WorkspaceRole.OWNER)
        )

        deployment = await api_db.compose_deployments.create(
            make_deployment(workspace.id, name="test-deploy")
        )

        await api_db.secrets.create(
            make_secret(str(deployment.id), key="API_KEY", value="secret-value")
        )

        response = await client.get(f"/v1/deployments/{deployment.id}/secrets")

        assert response.status_code == 200
        data = response.json()
        assert len(data["secrets"]) == 1
        assert data["secrets"][0]["key"] == "API_KEY"
        assert data["secrets"][0]["value"] == "● ● ● ● ● ● ● ●"

    async def test_list_secrets_shows_values_for_admin(
        self, client: AsyncClient, api_db: Database, api_user: User
    ):
        """Admin users can see secret values."""
        workspace = await api_db.workspaces.create(make_workspace())
        await api_db.user_workspaces.create(
            make_user_workspace(api_user.id, workspace.id, WorkspaceRole.OWNER)
        )

        deployment = await api_db.compose_deployments.create(
            make_deployment(workspace.id, name="test-deploy")
        )

        await api_db.secrets.create(
            make_secret(str(deployment.id), key="API_KEY", value="secret-value")
        )

        response = await client.get(
            f"/v1/deployments/{deployment.id}/secrets?show_values=true"
        )

        assert response.status_code == 200
        data = response.json()
        assert data["secrets"][0]["value"] == "secret-value"

    async def test_list_secrets_member_cannot_see_values(
        self, client: AsyncClient, api_db: Database, api_user: User
    ):
        """Members cannot see secret values even with show_values=true."""
        workspace = await api_db.workspaces.create(make_workspace())
        await api_db.user_workspaces.create(
            make_user_workspace(api_user.id, workspace.id, WorkspaceRole.MEMBER)
        )

        deployment = await api_db.compose_deployments.create(
            make_deployment(workspace.id, name="test-deploy")
        )

        response = await client.get(
            f"/v1/deployments/{deployment.id}/secrets?show_values=true"
        )

        assert response.status_code == 403

    async def test_list_secrets_empty_deployment(
        self, client: AsyncClient, api_db: Database, api_user: User
    ):
        """Deployment with no secrets returns empty list."""
        workspace = await api_db.workspaces.create(make_workspace())
        await api_db.user_workspaces.create(
            make_user_workspace(api_user.id, workspace.id, WorkspaceRole.OWNER)
        )

        deployment = await api_db.compose_deployments.create(
            make_deployment(workspace.id, name="test-deploy")
        )

        response = await client.get(f"/v1/deployments/{deployment.id}/secrets")

        assert response.status_code == 200
        assert response.json()["secrets"] == []


class TestGetSecretValue:
    """Tests for GET /v1/deployments/{id}/secrets/value/{key}."""

    async def test_get_secret_value_admin(
        self, client: AsyncClient, api_db: Database, api_user: User
    ):
        """Admin can get individual secret value."""
        workspace = await api_db.workspaces.create(make_workspace())
        await api_db.user_workspaces.create(
            make_user_workspace(api_user.id, workspace.id, WorkspaceRole.OWNER)
        )

        deployment = await api_db.compose_deployments.create(
            make_deployment(workspace.id, name="test-deploy")
        )

        await api_db.secrets.create(
            make_secret(str(deployment.id), key="API_KEY", value="secret-value")
        )

        response = await client.get(
            f"/v1/deployments/{deployment.id}/secrets/value/API_KEY"
        )

        assert response.status_code == 200
        # Endpoint returns str, FastAPI JSON-encodes it
        value = response.json()
        assert value == "secret-value"

    async def test_get_secret_value_not_found(
        self, client: AsyncClient, api_db: Database, api_user: User
    ):
        """Getting non-existent secret returns 404."""
        workspace = await api_db.workspaces.create(make_workspace())
        await api_db.user_workspaces.create(
            make_user_workspace(api_user.id, workspace.id, WorkspaceRole.OWNER)
        )

        deployment = await api_db.compose_deployments.create(
            make_deployment(workspace.id, name="test-deploy")
        )

        response = await client.get(
            f"/v1/deployments/{deployment.id}/secrets/value/NONEXISTENT"
        )

        assert response.status_code == 404

    async def test_get_secret_value_requires_admin(
        self, client: AsyncClient, api_db: Database, api_user: User
    ):
        """Members cannot get secret values."""
        workspace = await api_db.workspaces.create(make_workspace())
        await api_db.user_workspaces.create(
            make_user_workspace(api_user.id, workspace.id, WorkspaceRole.MEMBER)
        )

        deployment = await api_db.compose_deployments.create(
            make_deployment(workspace.id, name="test-deploy")
        )

        response = await client.get(
            f"/v1/deployments/{deployment.id}/secrets/value/API_KEY"
        )

        assert response.status_code == 403


class TestCreateSecrets:
    """Tests for POST /v1/deployments/{id}/secrets."""

    async def test_create_secrets_success(
        self, client: AsyncClient, api_db: Database, api_user: User
    ):
        """Successfully create secrets."""
        workspace = await api_db.workspaces.create(make_workspace())
        await api_db.user_workspaces.create(
            make_user_workspace(api_user.id, workspace.id, WorkspaceRole.OWNER)
        )

        deployment = await api_db.compose_deployments.create(
            make_deployment(workspace.id, name="test-deploy")
        )

        response = await client.post(
            f"/v1/deployments/{deployment.id}/secrets",
            json={
                "secrets": [
                    {
                        "key": "API_KEY",
                        "value": "secret-value",
                        "source": SecretSource.USER.value,
                    }
                ]
            },
        )

        assert response.status_code == 200
        data = response.json()
        assert data["secrets_count"] == 1

    async def test_create_secrets_duplicate_fails(
        self, client: AsyncClient, api_db: Database, api_user: User
    ):
        """Creating duplicate secret fails."""
        workspace = await api_db.workspaces.create(make_workspace())
        await api_db.user_workspaces.create(
            make_user_workspace(api_user.id, workspace.id, WorkspaceRole.OWNER)
        )

        deployment = await api_db.compose_deployments.create(
            make_deployment(workspace.id, name="test-deploy")
        )

        await api_db.secrets.create(
            make_secret(str(deployment.id), key="API_KEY", value="existing")
        )

        response = await client.post(
            f"/v1/deployments/{deployment.id}/secrets",
            json={
                "secrets": [
                    {
                        "key": "API_KEY",
                        "value": "new-value",
                        "source": SecretSource.USER.value,
                    }
                ]
            },
        )

        assert response.status_code == 409

    async def test_create_secrets_requires_admin(
        self, client: AsyncClient, api_db: Database, api_user: User
    ):
        """Only admins can create secrets."""
        workspace = await api_db.workspaces.create(make_workspace())
        await api_db.user_workspaces.create(
            make_user_workspace(api_user.id, workspace.id, WorkspaceRole.MEMBER)
        )

        deployment = await api_db.compose_deployments.create(
            make_deployment(workspace.id, name="test-deploy")
        )

        response = await client.post(
            f"/v1/deployments/{deployment.id}/secrets",
            json={
                "secrets": [
                    {
                        "key": "API_KEY",
                        "value": "secret",
                        "source": SecretSource.USER.value,
                    }
                ]
            },
        )

        assert response.status_code == 403


class TestUpdateSecrets:
    """Tests for PATCH /v1/deployments/{id}/secrets."""

    async def test_update_secrets_success(
        self, client: AsyncClient, api_db: Database, api_user: User
    ):
        """Successfully update existing secrets."""
        workspace = await api_db.workspaces.create(make_workspace())
        await api_db.user_workspaces.create(
            make_user_workspace(api_user.id, workspace.id, WorkspaceRole.OWNER)
        )

        deployment = await api_db.compose_deployments.create(
            make_deployment(workspace.id, name="test-deploy")
        )

        await api_db.secrets.create(
            make_secret(str(deployment.id), key="API_KEY", value="old-value")
        )

        response = await client.patch(
            f"/v1/deployments/{deployment.id}/secrets",
            json={
                "secrets": [
                    {
                        "key": "API_KEY",
                        "value": "new-value",
                        "source": SecretSource.USER.value,
                    }
                ]
            },
        )

        assert response.status_code == 200
        data = response.json()
        assert data["secrets_count"] == 1

    async def test_update_secrets_not_found_fails(
        self, client: AsyncClient, api_db: Database, api_user: User
    ):
        """Updating non-existent secret fails."""
        workspace = await api_db.workspaces.create(make_workspace())
        await api_db.user_workspaces.create(
            make_user_workspace(api_user.id, workspace.id, WorkspaceRole.OWNER)
        )

        deployment = await api_db.compose_deployments.create(
            make_deployment(workspace.id, name="test-deploy")
        )

        response = await client.patch(
            f"/v1/deployments/{deployment.id}/secrets",
            json={
                "secrets": [
                    {
                        "key": "NONEXISTENT",
                        "value": "value",
                        "source": SecretSource.USER.value,
                    }
                ]
            },
        )

        assert response.status_code == 404


class TestDeleteSecrets:
    """Tests for DELETE /v1/deployments/{id}/secrets."""

    async def test_delete_secrets_success(
        self, client: AsyncClient, api_db: Database, api_user: User
    ):
        """Successfully delete secrets."""
        workspace = await api_db.workspaces.create(make_workspace())
        await api_db.user_workspaces.create(
            make_user_workspace(api_user.id, workspace.id, WorkspaceRole.OWNER)
        )

        deployment = await api_db.compose_deployments.create(
            make_deployment(workspace.id, name="test-deploy")
        )

        await api_db.secrets.create(
            make_secret(str(deployment.id), key="API_KEY", value="value")
        )

        response = await client.request(
            "DELETE",
            f"/v1/deployments/{deployment.id}/secrets",
            content=json.dumps(
                {
                    "secrets": [
                        {
                            "key": "API_KEY",
                            "value": "",
                            "source": SecretSource.USER.value,
                        }
                    ]
                }
            ),
            headers={"Content-Type": "application/json"},
        )

        assert response.status_code == 200
        data = response.json()
        assert data["secrets_count"] == 0

    async def test_delete_secrets_not_found_fails(
        self, client: AsyncClient, api_db: Database, api_user: User
    ):
        """Deleting non-existent secret fails."""
        workspace = await api_db.workspaces.create(make_workspace())
        await api_db.user_workspaces.create(
            make_user_workspace(api_user.id, workspace.id, WorkspaceRole.OWNER)
        )

        deployment = await api_db.compose_deployments.create(
            make_deployment(workspace.id, name="test-deploy")
        )

        response = await client.request(
            "DELETE",
            f"/v1/deployments/{deployment.id}/secrets",
            content=json.dumps(
                {
                    "secrets": [
                        {
                            "key": "NONEXISTENT",
                            "value": "",
                            "source": SecretSource.USER.value,
                        }
                    ]
                }
            ),
            headers={"Content-Type": "application/json"},
        )

        assert response.status_code == 404
