"""Tests for API keys API routes."""

import pytest
from backend.database import Database
from backend.database.models import UserInDb
from httpx import AsyncClient
from models.api_keys import ApiKeyExpirationDays

from tests.fixtures.database import make_api_key, requires_db

pytestmark = [pytest.mark.asyncio, requires_db]


class TestListAPIKeys:
    """Tests for GET /v1/api-keys."""

    async def test_list_api_keys_returns_user_keys(
        self, client: AsyncClient, api_db: Database, api_user: UserInDb
    ):
        """User can list their own API keys."""
        await api_db.api_keys.create(make_api_key(str(api_user.id), name="my-key"))

        response = await client.get("/v1/api-keys")

        assert response.status_code == 200
        keys = response.json()
        assert len(keys) == 1
        assert keys[0]["name"] == "my-key"

    async def test_list_api_keys_empty(self, client: AsyncClient):
        """User with no API keys gets empty list."""
        response = await client.get("/v1/api-keys")

        assert response.status_code == 200
        keys = response.json()
        assert len(keys) == 0


class TestCreateAPIKey:
    """Tests for POST /v1/api-keys."""

    async def test_create_api_key_success(self, client: AsyncClient, api_db: Database):
        """User can create API key."""
        response = await client.post(
            "/v1/api-keys",
            json={
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
        self, client: AsyncClient, api_db: Database, api_user: UserInDb
    ):
        """Creating API key with duplicate name fails."""
        await api_db.api_keys.create(make_api_key(str(api_user.id), name="existing"))

        response = await client.post(
            "/v1/api-keys",
            json={
                "name": "existing",
                "expires_at": ApiKeyExpirationDays.THIRTY_DAYS.value,
            },
        )

        assert response.status_code == 409


class TestUpdateAPIKey:
    """Tests for PUT /v1/api-keys/{id}."""

    async def test_update_api_key_success(
        self, client: AsyncClient, api_db: Database, api_user: UserInDb
    ):
        """User can update their own API key."""
        api_key = await api_db.api_keys.create(
            make_api_key(str(api_user.id), name="old-name")
        )
        old_value = api_key.value

        response = await client.put(
            f"/v1/api-keys/{api_key.id}",
            json={
                "expires_at": ApiKeyExpirationDays.THIRTY_DAYS.value,
            },
        )

        assert response.status_code == 200
        data = response.json()
        assert "value" in data
        assert data["value"] != old_value

    async def test_update_nonexistent_api_key_fails(self, client: AsyncClient):
        """Updating non-existent API key returns 404."""
        response = await client.put(
            "/v1/api-keys/00000000-0000-0000-0000-000000000000",
            json={
                "expires_at": ApiKeyExpirationDays.THIRTY_DAYS.value,
            },
        )

        assert response.status_code == 404


class TestDeleteAPIKey:
    """Tests for DELETE /v1/api-keys/{id}."""

    async def test_delete_api_key_success(
        self, client: AsyncClient, api_db: Database, api_user: UserInDb
    ):
        """User can delete their own API key."""
        api_key = await api_db.api_keys.create(
            make_api_key(str(api_user.id), name="to-delete")
        )

        response = await client.delete(f"/v1/api-keys/{api_key.id}")

        assert response.status_code == 200
        data = response.json()
        assert data["id"] == str(api_key.id)

    async def test_delete_nonexistent_api_key_fails(self, client: AsyncClient):
        """Deleting non-existent API key returns 404."""
        response = await client.delete(
            "/v1/api-keys/00000000-0000-0000-0000-000000000000"
        )

        assert response.status_code == 404
