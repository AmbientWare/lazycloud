"""Tests for workspace API routes."""

import pytest
from backend.database import Database
from backend.database.users import UserPydantic
from httpx import AsyncClient
from models.workspaces import WorkspaceRole

from tests.fixtures.database import (
    make_user,
    make_user_workspace,
    make_workspace,
    requires_db,
)

pytestmark = [pytest.mark.asyncio, requires_db]


class TestListWorkspaces:
    """Tests for GET /v1/workspaces."""

    async def test_list_workspaces_empty(self, client: AsyncClient):
        """User with no workspaces gets empty list."""
        response = await client.get("/v1/workspaces")

        assert response.status_code == 200
        assert response.json() == []

    async def test_list_workspaces_returns_user_workspaces(
        self, client: AsyncClient, api_db: Database, api_user: UserPydantic
    ):
        """User sees only their workspaces."""
        workspace = await api_db.workspaces.create(make_workspace(name="My Workspace"))
        await api_db.user_workspaces.create(
            make_user_workspace(api_user.id, workspace.id, WorkspaceRole.OWNER)
        )

        response = await client.get("/v1/workspaces")

        assert response.status_code == 200
        data = response.json()
        assert len(data) == 1
        assert data[0]["name"] == "My Workspace"
        assert data[0]["role"] == WorkspaceRole.OWNER.value

    async def test_list_workspaces_multiple(
        self, client: AsyncClient, api_db: Database, api_user: UserPydantic
    ):
        """User with multiple workspaces sees all of them."""
        ws1 = await api_db.workspaces.create(make_workspace(name="Workspace 1"))
        ws2 = await api_db.workspaces.create(make_workspace(name="Workspace 2"))
        await api_db.user_workspaces.create(
            make_user_workspace(api_user.id, ws1.id, WorkspaceRole.OWNER)
        )
        await api_db.user_workspaces.create(
            make_user_workspace(api_user.id, ws2.id, WorkspaceRole.MEMBER)
        )

        response = await client.get("/v1/workspaces")

        assert response.status_code == 200
        data = response.json()
        assert len(data) == 2
        names = {ws["name"] for ws in data}
        assert names == {"Workspace 1", "Workspace 2"}

    async def test_list_workspaces_excludes_other_users(
        self, client: AsyncClient, api_db: Database
    ):
        """User doesn't see workspaces they don't belong to."""
        other_user = await api_db.users.create(make_user("other"))
        workspace = await api_db.workspaces.create(make_workspace(name="Other's WS"))
        await api_db.user_workspaces.create(
            make_user_workspace(other_user.id, workspace.id, WorkspaceRole.OWNER)
        )

        response = await client.get("/v1/workspaces")

        assert response.status_code == 200
        assert response.json() == []


class TestCreateWorkspace:
    """Tests for POST /v1/workspaces."""

    async def test_create_workspace_success(
        self, client: AsyncClient, api_db: Database, api_user: UserPydantic
    ):
        """Successfully create a new workspace."""
        response = await client.post("/v1/workspaces", json={"name": "New Workspace"})

        assert response.status_code == 200
        data = response.json()
        assert data["name"] == "New Workspace"
        assert data["is_personal"] is False
        assert data["role"] == WorkspaceRole.OWNER.value

        # Verify in database
        workspace = await api_db.workspaces.get_by_id(data["id"])
        assert workspace is not None
        assert workspace.name == "New Workspace"

    async def test_create_workspace_invalid_name_rejected(self, client: AsyncClient):
        """Invalid workspace names are rejected."""
        response = await client.post("/v1/workspaces", json={"name": ""})
        assert response.status_code == 400

    async def test_create_workspace_special_chars_rejected(self, client: AsyncClient):
        """Workspace names with special chars are rejected."""
        response = await client.post("/v1/workspaces", json={"name": "Test@Workspace!"})
        assert response.status_code == 400


class TestWorkspaceResponse:
    """Tests for workspace response structure."""

    async def test_workspace_response_has_required_fields(
        self, client: AsyncClient, api_db: Database, api_user: UserPydantic
    ):
        """Workspace response includes all required fields."""
        workspace = await api_db.workspaces.create(make_workspace())
        await api_db.user_workspaces.create(
            make_user_workspace(api_user.id, workspace.id, WorkspaceRole.OWNER)
        )

        response = await client.get("/v1/workspaces")

        assert response.status_code == 200
        ws = response.json()[0]
        assert "id" in ws
        assert "name" in ws
        assert "is_personal" in ws
        assert "role" in ws

    async def test_personal_workspace_flag(
        self, client: AsyncClient, api_db: Database, api_user: UserPydantic
    ):
        """Personal workspace is correctly flagged."""
        personal = await api_db.workspaces.create(
            make_workspace(name="Personal", is_personal=True)
        )
        await api_db.user_workspaces.create(
            make_user_workspace(api_user.id, personal.id, WorkspaceRole.OWNER)
        )

        response = await client.get("/v1/workspaces")

        assert response.status_code == 200
        ws = response.json()[0]
        assert ws["is_personal"] is True
