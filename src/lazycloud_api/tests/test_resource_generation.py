"""
Tests for resource request auto-generation.
"""

from lazycloud_api.services.compose.models import ResourceConfig, ResourcesConfig
from lazycloud_api.services.k8s.workloads import (
    _calculate_memory_request,
    generate_resources_values,
)


class TestResourceGeneration:
    """Test cases for resource generation with auto-requests."""

    def test_limits_only_generates_requests(self):
        """Test that requests are auto-generated when only limits are specified."""
        resources_config = ResourcesConfig(
            limits=ResourceConfig(cpus="2", memory="1Gi")
        )

        result = generate_resources_values(resources_config)

        assert result is not None
        assert result.limits is not None
        assert result.requests is not None

        # Check CPU request is 50% of limit
        assert result.limits.cpu == "2"
        assert result.requests.cpu == "1.0"

        # Check memory request is 80% of limit
        assert result.limits.memory == "1Gi"
        assert result.requests.memory == "0.8Gi"

    def test_millicores_handling(self):
        """Test CPU millicores are handled correctly."""
        resources_config = ResourcesConfig(limits=ResourceConfig(cpus="500m"))

        result = generate_resources_values(resources_config)

        assert result.limits.cpu == "500m"
        assert result.requests.cpu == "250m"  # 50% of 500m

    def test_explicit_requests_not_overridden(self):
        """Test that explicit requests are not overridden."""
        resources_config = ResourcesConfig(
            limits=ResourceConfig(cpus="2", memory="1Gi"),
            reservations=ResourceConfig(cpus="0.5", memory="256Mi"),
        )

        result = generate_resources_values(resources_config)

        assert result is not None
        # Should use explicit requests, not auto-generated ones
        assert result.requests.cpu == "0.5"
        assert result.requests.memory == "256Mi"

    def test_memory_calculation_different_units(self):
        """Test memory request calculation with different units."""
        test_cases = [
            ("1Gi", "0.8Gi"),
            ("2G", "1.6G"),
            ("512Mi", "409Mi"),  # 80% of 512
            ("1024M", "819M"),  # 80% of 1024
            ("2048Ki", "1638Ki"),  # 80% of 2048
            ("4096K", "3276K"),  # 80% of 4096
        ]

        for limit, expected_request in test_cases:
            result = _calculate_memory_request(limit, 0.8)
            assert result == expected_request, (
                f"Failed for {limit}: got {result}, expected {expected_request}"
            )

    def test_no_resources_returns_none(self):
        """Test that empty resources config returns None."""
        resources_config = ResourcesConfig()
        assert generate_resources_values(resources_config) is None

    def test_only_memory_limit(self):
        """Test with only memory limit specified."""
        resources_config = ResourcesConfig(limits=ResourceConfig(memory="2Gi"))

        result = generate_resources_values(resources_config)

        assert result.limits is not None
        assert result.requests is not None
        assert result.requests.memory == "1.6Gi"
        # CPU should not be in requests since it wasn't in limits
        assert result.requests.cpu is None
