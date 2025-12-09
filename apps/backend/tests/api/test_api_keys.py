"""Tests for API keys API routes (admin only)."""

import pytest
from backend.database import Database
from httpx import AsyncClient
from models.api_keys import ApiKeyExpirationDays

from tests.fixtures.database import make_api_key, make_user, requires_db

pytestmark = [pytest.mark.asyncio, requires_db]


class TestListAPIKeys:
    """Tests for GET /v1/api-keys."""

    async def test_list_api_keys_admin(
        self, admin_client: AsyncClient, api_db: Database
    ):
        """Admin can list all API keys."""
        other_user = await api_db.users.create(make_user("other"))
        await api_db.api_keys.create(make_api_key(str(other_user.id), name="other-key"))

        response = await admin_client.get("/v1/api-keys")

        assert response.status_code == 200
        keys = response.json()
        assert len(keys) >= 1

    async def test_list_api_keys_filter_by_user(
        self, admin_client: AsyncClient, api_db: Database
    ):
        """Admin can filter API keys by user."""
        user = await api_db.users.create(make_user("test"))
        await api_db.api_keys.create(make_api_key(str(user.id), name="user-key"))

        response = await admin_client.get(f"/v1/api-keys?user_id={user.workos_id}")

        assert response.status_code == 200
        keys = response.json()
        assert len(keys) == 1
        assert keys[0]["name"] == "user-key"


class TestCreateAPIKey:
    """Tests for POST /v1/api-keys."""

    async def test_create_api_key_success(
        self, admin_client: AsyncClient, api_db: Database
    ):
        """Admin can create API key."""
        user = await api_db.users.create(make_user("test"))

        response = await admin_client.post(
            "/v1/api-keys",
            json={
                "workos_id": user.workos_id,
                "name": "test-key",
                "expires_at": ApiKeyExpirationDays.THIRTY_DAYS.value,
            },
        )

        assert response.status_code == 200
        data = response.json()
        assert data["name"] == "test-key"
        assert "value" in data
        assert data["value"].startswith("sk_")

    async def test_create_api_key_duplicate_name_fails(
        self, admin_client: AsyncClient, api_db: Database
    ):
        """Creating API key with duplicate name fails."""
        user = await api_db.users.create(make_user("test"))
        await api_db.api_keys.create(make_api_key(str(user.id), name="existing"))

        response = await admin_client.post(
            "/v1/api-keys",
            json={
                "workos_id": user.workos_id,
                "name": "existing",
                "expires_at": ApiKeyExpirationDays.THIRTY_DAYS.value,
            },
        )

        assert response.status_code == 409


class TestUpdateAPIKey:
    """Tests for PUT /v1/api-keys/{id}."""

    async def test_update_api_key_success(
        self, admin_client: AsyncClient, api_db: Database
    ):
        """Admin can update API key."""
        user = await api_db.users.create(make_user("test"))
        api_key = await api_db.api_keys.create(
            make_api_key(str(user.id), name="old-name")
        )

        response = await admin_client.put(
            f"/v1/api-keys/{api_key.id}",
            json={
                "workos_id": user.workos_id,
                "expires_at": ApiKeyExpirationDays.THIRTY_DAYS.value,
            },
        )

        assert response.status_code == 200
        data = response.json()
        assert "value" in data
        assert data["value"] != api_key.value


class TestDeleteAPIKeys:
    """Tests for DELETE /v1/api-keys."""

    async def test_delete_api_key_by_id(
        self, admin_client: AsyncClient, api_db: Database
    ):
        """Admin can delete API key by ID."""
        user = await api_db.users.create(make_user("test"))
        api_key = await api_db.api_keys.create(
            make_api_key(str(user.id), name="to-delete")
        )

        response = await admin_client.delete(f"/v1/api-keys?api_key_id={api_key.id}")

        assert response.status_code == 200
        deleted = response.json()
        assert len(deleted) == 1
        assert deleted[0]["id"] == str(api_key.id)
