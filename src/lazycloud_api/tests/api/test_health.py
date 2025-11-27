"""Tests for health and version API routes."""

import pytest
from httpx import AsyncClient

from lazycloud_api.tests.fixtures.database import requires_db

pytestmark = [pytest.mark.asyncio, requires_db]


class TestHealthCheck:
    """Tests for GET /health."""

    async def test_health_returns_ok(self, client: AsyncClient):
        """Basic health check returns ok status."""
        response = await client.get("/health")

        assert response.status_code == 200
        assert response.json() == {"status": "ok"}


class TestCLIVersion:
    """Tests for GET /cli-version."""

    async def test_cli_version_returns_version(self, client: AsyncClient):
        """CLI version endpoint returns a version string."""
        response = await client.get("/cli-version")

        assert response.status_code == 200
        data = response.json()
        assert "version" in data
        assert isinstance(data["version"], str)

    async def test_cli_version_format(self, client: AsyncClient):
        """CLI version has semver-like format."""
        response = await client.get("/cli-version")

        assert response.status_code == 200
        version = response.json()["version"]
        # Basic version format check (x.y.z)
        parts = version.split(".")
        assert len(parts) >= 2  # At least major.minor
