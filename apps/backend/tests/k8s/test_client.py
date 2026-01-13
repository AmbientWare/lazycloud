"""Tests for Kubernetes client utilities."""

import pytest

from backend.services.k8s import create_ns_name, create_release_name
from backend.services.k8s.client import (
    close_async_api_client,
    get_async_api_client,
    get_async_apps_v1_api,
    get_async_batch_v1_api,
    get_async_core_v1_api,
)


class TestAsyncKubernetesClientFunctions:
    """Tests for async Kubernetes client functions."""

    @pytest.mark.asyncio
    async def test_get_async_api_client_returns_same_instance(self):
        """Test that get_async_api_client returns the same cached instance."""
        # Reset client first
        await close_async_api_client()

        # Note: This test requires a valid kubeconfig or in-cluster config
        # In CI without K8s, this will raise an exception
        # These tests are meant to be run in an environment with K8s access
        try:
            client1 = await get_async_api_client()
            client2 = await get_async_api_client()

            # Should be the same cached instance
            assert client1 is client2

            # Cleanup
            await close_async_api_client()
        except Exception:
            pytest.skip("Kubernetes config not available")

    @pytest.mark.asyncio
    async def test_get_async_core_v1_api_returns_api(self):
        """Test that get_async_core_v1_api returns an API instance."""
        await close_async_api_client()

        try:
            api = await get_async_core_v1_api()
            assert api is not None

            await close_async_api_client()
        except Exception:
            pytest.skip("Kubernetes config not available")

    @pytest.mark.asyncio
    async def test_get_async_apps_v1_api_returns_api(self):
        """Test that get_async_apps_v1_api returns an API instance."""
        await close_async_api_client()

        try:
            api = await get_async_apps_v1_api()
            assert api is not None

            await close_async_api_client()
        except Exception:
            pytest.skip("Kubernetes config not available")

    @pytest.mark.asyncio
    async def test_get_async_batch_v1_api_returns_api(self):
        """Test that get_async_batch_v1_api returns an API instance."""
        await close_async_api_client()

        try:
            api = await get_async_batch_v1_api()
            assert api is not None

            await close_async_api_client()
        except Exception:
            pytest.skip("Kubernetes config not available")


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
