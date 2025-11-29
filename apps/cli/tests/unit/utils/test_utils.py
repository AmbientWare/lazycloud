"""Unit tests for utility functions"""

import pytest

from cli.utils.utils import format_image_name, format_cpu, format_memory


@pytest.mark.unit
class TestFormatImageName:
    """Tests for format_image_name function"""

    def test_format_image_with_registry(self):
        """Test extracting image name from full registry path"""
        assert format_image_name("gcr.io/project/my-app:latest") == "my-app:latest"
        assert format_image_name("docker.io/library/nginx:1.21") == "nginx:1.21"

    def test_format_image_without_registry(self):
        """Test image name without registry"""
        assert format_image_name("nginx:latest") == "nginx:latest"
        assert format_image_name("redis") == "redis"

    def test_format_image_empty(self):
        """Test empty image string"""
        assert format_image_name("") == ""


@pytest.mark.unit
class TestFormatCPU:
    """Tests for format_cpu function"""

    def test_format_cpu_millicores(self):
        """Test formatting CPU in millicores"""
        assert format_cpu("100m") == "0.10"
        assert format_cpu("500m") == "0.50"
        assert format_cpu("1000m") == "1.0"
        assert format_cpu("50m") == "0.050"

    def test_format_cpu_cores(self):
        """Test formatting CPU in cores"""
        assert format_cpu("0.5") == "0.50"
        assert format_cpu("2") == "2.0"
        assert format_cpu("1") == "1.0"

    def test_format_cpu_na(self):
        """Test handling N/A CPU values"""
        assert format_cpu("N/A") == "N/A"
        assert format_cpu("") == "N/A"
        assert format_cpu(None) == "N/A"

    def test_format_cpu_invalid(self):
        """Test handling invalid CPU values"""
        assert format_cpu("invalid") == "invalid"
        assert format_cpu("abc") == "abc"


@pytest.mark.unit
class TestFormatMemory:
    """Tests for format_memory function"""

    def test_format_memory_bytes(self):
        """Test formatting memory in bytes"""
        result = format_memory("1073741824")  # 1 GiB
        assert "Gi" in result or "GB" in result or "G" in result

    def test_format_memory_with_units(self):
        """Test formatting memory with units"""
        result = format_memory("256Mi")
        assert result  # Should return formatted string

    def test_format_memory_na(self):
        """Test handling N/A memory values"""
        assert format_memory("N/A") == "N/A"
        assert format_memory("") == "N/A"
