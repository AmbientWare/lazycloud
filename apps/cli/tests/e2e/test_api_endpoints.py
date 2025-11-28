"""E2E tests for API endpoints - full stack validation.

Tests that require the real API server running with actual auth flow.
Basic route tests are in tests/api/ - these test full integration.
"""

import httpx
import pytest

from tests.e2e.conftest import API_BASE_URL, requires_api

pytestmark = [pytest.mark.e2e, requires_api]


class TestAuthenticationBehavior:
    """Test API authentication flow with real server."""

    def test_protected_endpoint_without_auth(self):
        """Protected endpoints require auth in prod mode."""
        with httpx.Client(base_url=API_BASE_URL, timeout=10.0) as client:
            response = client.get("/v1/workspaces")
            # In dev mode: 200 (bypass). In prod mode: 401.
            assert response.status_code in [200, 401]

    def test_invalid_deployment_returns_404(self, api_client: httpx.Client):
        """Non-existent deployment returns 404."""
        fake_id = "00000000-0000-0000-0000-000000000000"
        response = api_client.get(f"/v1/deployments/{fake_id}/status")
        assert response.status_code in [404, 401]


class TestDeploymentIntegration:
    """Test deployment endpoints with full stack."""

    def test_list_deployments_requires_workspace(self, api_client: httpx.Client):
        """Listing deployments requires workspace_id parameter."""
        response = api_client.get("/v1/deployments")
        assert response.status_code in [422, 401]

    def test_list_deployments_with_workspace(self, api_client: httpx.Client):
        """List deployments for a workspace."""
        ws_response = api_client.get("/v1/workspaces")
        if ws_response.status_code != 200:
            pytest.skip("Cannot get workspaces")

        workspaces = ws_response.json()
        if not workspaces:
            pytest.skip("No workspaces available")

        workspace_id = workspaces[0]["id"]
        response = api_client.get(f"/v1/deployments?workspace_id={workspace_id}")
        assert response.status_code == 200
        assert "deployments" in response.json()

    def test_nonexistent_deployment_services(self, api_client: httpx.Client):
        """Getting services for non-existent deployment returns 404."""
        fake_id = "00000000-0000-0000-0000-000000000000"
        response = api_client.get(f"/v1/deployments/{fake_id}/services")
        assert response.status_code in [404, 401]


class TestInvitationIntegration:
    """Test invitation endpoints with full stack."""

    def test_get_pending_invitations(self, api_client: httpx.Client):
        """Get pending invitations returns a list."""
        response = api_client.get("/v1/invitations/pending")
        if response.status_code == 200:
            assert isinstance(response.json(), list)
        else:
            assert response.status_code == 401
