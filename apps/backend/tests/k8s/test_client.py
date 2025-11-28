"""Tests for Kubernetes client utilities."""

from unittest.mock import MagicMock, patch

from backend.services.k8s import create_ns_name, create_release_name
from backend.services.k8s.client import (
    get_apps_v1_api,
    get_batch_v1_api,
    get_core_v1_api,
)


class TestKubernetesClientFunctions:
    """Tests for Kubernetes client functions."""

    def test_get_core_v1_api_caches(self):
        """Test that get_core_v1_api returns cached instance."""
        # Clear the cache first

        get_core_v1_api.cache_clear()

        with patch("backend.services.k8s.client._get_api_client") as mock_client:
            mock_client.return_value = MagicMock()

            api1 = get_core_v1_api()
            api2 = get_core_v1_api()

            # Should be the same cached instance
            assert api1 is api2
            # Should only call _get_api_client once
            mock_client.assert_called_once()

            # Clean up cache
            get_core_v1_api.cache_clear()

    def test_get_apps_v1_api_caches(self):
        """Test that get_apps_v1_api returns cached instance."""

        get_apps_v1_api.cache_clear()

        with patch("backend.services.k8s.client._get_api_client") as mock_client:
            mock_client.return_value = MagicMock()

            api1 = get_apps_v1_api()
            api2 = get_apps_v1_api()

            assert api1 is api2
            mock_client.assert_called_once()

            get_apps_v1_api.cache_clear()

    def test_get_batch_v1_api_caches(self):
        """Test that get_batch_v1_api returns cached instance."""

        get_batch_v1_api.cache_clear()

        with patch("backend.services.k8s.client._get_api_client") as mock_client:
            mock_client.return_value = MagicMock()

            api1 = get_batch_v1_api()
            api2 = get_batch_v1_api()

            assert api1 is api2
            mock_client.assert_called_once()

            get_batch_v1_api.cache_clear()


class TestNamespaceUtilities:
    """Tests for namespace-related utilities."""

    def test_create_ns_name_format(self):
        """Test namespace name creation follows expected format."""

        workspace_id = "12345678-1234-1234-1234-123456789012"
        ns_name = create_ns_name(workspace_id)

        assert ns_name.startswith("lc-")
        assert len(ns_name) <= 63

    def test_create_ns_name_deterministic(self):
        """Test namespace name is deterministic."""

        workspace_id = "12345678-1234-1234-1234-123456789012"
        ns1 = create_ns_name(workspace_id)
        ns2 = create_ns_name(workspace_id)

        assert ns1 == ns2


class TestReleaseNameUtilities:
    """Tests for Helm release name creation."""

    def test_create_release_name_max_length(self):
        """Test release name stays within 53 char Helm limit."""

        workspace_id = "de38749e-f166-48c9-ad44-f9f0ace222be"
        deployment_name = "my-very-long-deployment-name-that-could-exceed-limits"

        release_name = create_release_name(workspace_id, deployment_name)

        assert len(release_name) <= 53, f"Release name too long: {len(release_name)}"

    def test_create_release_name_format(self):
        """Test release name format is lc-{prefix}-{name}."""

        workspace_id = "de38749e-f166-48c9-ad44-f9f0ace222be"
        deployment_name = "my-app"

        release_name = create_release_name(workspace_id, deployment_name)

        assert release_name.startswith("lc-")
        assert "my-app" in release_name
        assert release_name == release_name.lower()

    def test_create_release_name_deterministic(self):
        """Test release name is deterministic."""

        workspace_id = "de38749e-f166-48c9-ad44-f9f0ace222be"
        deployment_name = "test-deploy"

        name1 = create_release_name(workspace_id, deployment_name)
        name2 = create_release_name(workspace_id, deployment_name)

        assert name1 == name2

    def test_create_release_name_no_trailing_hyphen(self):
        """Test release name doesn't end with hyphen after truncation."""

        workspace_id = "12345678-1234-1234-1234-123456789012"
        # Name that would end in hyphen after truncation
        deployment_name = "a" * 50 + "-"

        release_name = create_release_name(workspace_id, deployment_name)

        assert not release_name.endswith("-")
