"""Tests for converter functions."""

from backend.services.k8s.generators.converters import (
    convert_cpu_value,
    convert_memory_value,
    is_kubernetes_name_compliant,
    parse_duration,
    sanitize_name,
)


class TestMemoryConversion:
    """Tests for memory value conversion."""

    def test_convert_docker_m_to_mi(self):
        """Test converting Docker M to Kubernetes Mi."""
        assert convert_memory_value("512M") == "512Mi"

    def test_convert_docker_g_to_gi(self):
        """Test converting Docker G to Kubernetes Gi."""
        assert convert_memory_value("1G") == "1Gi"

    def test_convert_docker_k_to_ki(self):
        """Test converting Docker K to Kubernetes Ki."""
        assert convert_memory_value("256K") == "256Ki"

    def test_preserve_kubernetes_mi(self):
        """Test Kubernetes Mi format is preserved."""
        assert convert_memory_value("512Mi") == "512Mi"

    def test_preserve_kubernetes_gi(self):
        """Test Kubernetes Gi format is preserved."""
        assert convert_memory_value("1Gi") == "1Gi"

    def test_preserve_kubernetes_ki(self):
        """Test Kubernetes Ki format is preserved."""
        assert convert_memory_value("256Ki") == "256Ki"

    def test_preserve_kubernetes_ti(self):
        """Test Kubernetes Ti format is preserved."""
        assert convert_memory_value("1Ti") == "1Ti"

    def test_convert_lowercase_to_uppercase(self):
        """Test lowercase units are converted to uppercase."""
        assert convert_memory_value("512m") == "512Mi"
        assert convert_memory_value("1g") == "1Gi"
        assert convert_memory_value("256k") == "256Ki"

    def test_convert_bytes_to_gi(self):
        """Test bytes to Gi conversion."""
        # 1 GiB in bytes
        result = convert_memory_value(1024 * 1024 * 1024)
        assert result == "1Gi"

    def test_convert_bytes_to_mi(self):
        """Test bytes to Mi conversion."""
        # 512 MiB in bytes
        result = convert_memory_value(512 * 1024 * 1024)
        assert result == "512Mi"

    def test_convert_bytes_to_ki(self):
        """Test bytes to Ki conversion."""
        # 256 KiB in bytes
        result = convert_memory_value(256 * 1024)
        assert result == "256Ki"

    def test_small_bytes_stay_as_bytes(self):
        """Test small byte values stay as bytes."""
        result = convert_memory_value(512)
        assert result == "512"

    def test_string_bytes_preserved(self):
        """Test string byte values are preserved."""
        result = convert_memory_value("1048576")
        assert result == "1048576"


class TestCPUConversion:
    """Tests for CPU value conversion."""

    def test_convert_string_preserved(self):
        """Test string CPU values are preserved."""
        assert convert_cpu_value("0.5") == "0.5"
        assert convert_cpu_value("1") == "1"
        assert convert_cpu_value("2.5") == "2.5"

    def test_convert_float_to_string(self):
        """Test float CPU values are converted to string."""
        assert convert_cpu_value(0.5) == "0.5"
        assert convert_cpu_value(1.0) == "1.0"
        assert convert_cpu_value(2.5) == "2.5"

    def test_convert_int_to_string(self):
        """Test int CPU values are converted to string."""
        assert convert_cpu_value(1) == "1"
        assert convert_cpu_value(2) == "2"
        assert convert_cpu_value(4) == "4"

    def test_convert_millicores_preserved(self):
        """Test millicore values are preserved."""
        assert convert_cpu_value("500m") == "500m"
        assert convert_cpu_value("1000m") == "1000m"


class TestDurationParsing:
    """Tests for duration string parsing."""

    def test_parse_seconds(self):
        """Test parsing seconds."""
        assert parse_duration("30s") == 30
        assert parse_duration("60s") == 60
        assert parse_duration("120s") == 120

    def test_parse_minutes(self):
        """Test parsing minutes."""
        assert parse_duration("5m") == 300
        assert parse_duration("10m") == 600

    def test_parse_hours(self):
        """Test parsing hours."""
        assert parse_duration("1h") == 3600
        assert parse_duration("2h") == 7200

    def test_parse_plain_number(self):
        """Test parsing plain number as seconds."""
        assert parse_duration("60") == 60
        assert parse_duration("120") == 120

    def test_parse_empty_string(self):
        """Test parsing empty string returns 0."""
        assert parse_duration("") == 0

    def test_parse_invalid_returns_zero(self):
        """Test parsing invalid duration returns 0."""
        assert parse_duration("invalid") == 0


class TestKubernetesNameCompliance:
    """Tests for Kubernetes name compliance checking."""

    def test_valid_names(self):
        """Test valid Kubernetes names pass."""
        valid_names = [
            "web",
            "api",
            "my-service",
            "service-123",
            "a",
            "a" * 63,
            "a1b2c3",
        ]

        for name in valid_names:
            assert is_kubernetes_name_compliant(name), f"'{name}' should be valid"

    def test_invalid_uppercase(self):
        """Test uppercase names fail."""
        assert not is_kubernetes_name_compliant("MyService")
        assert not is_kubernetes_name_compliant("SERVICE")

    def test_invalid_underscore(self):
        """Test names with underscore fail."""
        assert not is_kubernetes_name_compliant("my_service")

    def test_invalid_dot(self):
        """Test names with dot fail."""
        assert not is_kubernetes_name_compliant("my.service")

    def test_invalid_starts_with_number(self):
        """Test names starting with number fail."""
        assert not is_kubernetes_name_compliant("123service")

    def test_invalid_starts_with_hyphen(self):
        """Test names starting with hyphen fail."""
        assert not is_kubernetes_name_compliant("-service")

    def test_invalid_ends_with_hyphen(self):
        """Test names ending with hyphen fail."""
        assert not is_kubernetes_name_compliant("service-")

    def test_invalid_too_long(self):
        """Test names exceeding 63 chars fail."""
        assert not is_kubernetes_name_compliant("a" * 64)

    def test_invalid_empty(self):
        """Test empty names fail."""
        assert not is_kubernetes_name_compliant("")

    def test_custom_max_length(self):
        """Test custom max length."""
        name = "a" * 50
        assert is_kubernetes_name_compliant(name, max_length=50)
        assert not is_kubernetes_name_compliant(name, max_length=49)


class TestNameSanitization:
    """Tests for name sanitization."""

    def test_lowercase_conversion(self):
        """Test uppercase is converted to lowercase."""
        # sanitize_name only lowercases, doesn't insert hyphens at camelCase boundaries
        assert sanitize_name("MyService") == "myservice"
        # Underscores and other invalid chars become hyphens
        assert sanitize_name("My_Service") == "my-service"
        assert sanitize_name("SERVICE") == "service"

    def test_underscore_replacement(self):
        """Test underscore is replaced with hyphen."""
        assert sanitize_name("my_service") == "my-service"
        assert sanitize_name("my_long_service_name") == "my-long-service-name"

    def test_dot_replacement(self):
        """Test dot is replaced with hyphen."""
        assert sanitize_name("my.service") == "my-service"

    def test_leading_hyphen_removal(self):
        """Test leading hyphens are removed."""
        assert sanitize_name("-service") == "service"
        assert sanitize_name("---service") == "service"

    def test_trailing_hyphen_removal(self):
        """Test trailing hyphens are removed."""
        assert sanitize_name("service-") == "service"
        assert sanitize_name("service---") == "service"

    def test_number_prefix_handling(self):
        """Test names starting with number get prefix."""
        assert sanitize_name("123service") == "n123service"

    def test_truncation(self):
        """Test long names are truncated."""
        long_name = "a" * 100
        result = sanitize_name(long_name)
        assert len(result) <= 63

    def test_truncation_removes_trailing_hyphen(self):
        """Test truncation removes trailing hyphens."""
        # Name that would end in hyphen after truncation
        name = "a" * 62 + "-x"
        result = sanitize_name(name, max_length=63)
        assert not result.endswith("-")

    def test_empty_string_default(self):
        """Test empty string returns default."""
        assert sanitize_name("") == "default"

    def test_all_invalid_chars_default(self):
        """Test name with all invalid chars returns default."""
        assert sanitize_name("!!!") == "default"

    def test_complex_sanitization(self):
        """Test complex sanitization scenarios."""
        assert sanitize_name("My_Service.Name") == "my-service-name"
        assert sanitize_name("__service__") == "service"
        assert sanitize_name("UPPER_CASE_SERVICE") == "upper-case-service"


class TestParameterizedConversions:
    """Parameterized tests for conversions using fixtures."""

    def test_memory_conversions(self, memory_conversion_cases: list[tuple[str, str]]):
        """Test memory conversion cases from fixture."""
        for input_val, expected in memory_conversion_cases:
            result = convert_memory_value(input_val)
            assert result == expected, (
                f"convert_memory_value('{input_val}') = '{result}', expected '{expected}'"
            )

    def test_cpu_conversions(
        self, cpu_conversion_cases: list[tuple[str | float | int, str]]
    ):
        """Test CPU conversion cases from fixture."""
        for input_val, expected in cpu_conversion_cases:
            result = convert_cpu_value(input_val)
            assert result == expected, (
                f"convert_cpu_value('{input_val}') = '{result}', expected '{expected}'"
            )

    def test_duration_parsing(self, duration_parse_cases: list[tuple[str, int]]):
        """Test duration parsing cases from fixture."""
        for input_val, expected in duration_parse_cases:
            result = parse_duration(input_val)
            assert result == expected, (
                f"parse_duration('{input_val}') = {result}, expected {expected}"
            )
