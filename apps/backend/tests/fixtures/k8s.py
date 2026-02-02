"""K8s test fixtures."""

import pytest
from backend.database.models import ComposeDeployment, Secret
from backend.services.k8s.helm_values_generator import HelmValuesGenerator


@pytest.fixture
def helm_generator(test_deployment: ComposeDeployment) -> HelmValuesGenerator:
    """Create a HelmValuesGenerator instance."""
    return HelmValuesGenerator(test_deployment, [])


@pytest.fixture
def helm_generator_with_secrets(
    test_deployment: ComposeDeployment,
    test_secret: Secret,
) -> HelmValuesGenerator:
    """Create a HelmValuesGenerator instance with secrets."""
    return HelmValuesGenerator(test_deployment, [test_secret])


@pytest.fixture
def memory_conversion_cases() -> list[tuple[str, str]]:
    """Test cases for memory conversion (input, expected)."""
    return [
        ("512M", "512Mi"),
        ("1G", "1Gi"),
        ("256K", "256Ki"),
        ("512Mi", "512Mi"),
        ("1Gi", "1Gi"),
        ("2048", "2048"),
    ]


@pytest.fixture
def cpu_conversion_cases() -> list[tuple[str | float | int, str]]:
    """Test cases for CPU conversion (input, expected)."""
    return [
        ("0.5", "0.5"),
        ("1", "1"),
        ("2.5", "2.5"),
        (0.5, "0.5"),
        (1, "1"),
        (2, "2"),
    ]


@pytest.fixture
def duration_parse_cases() -> list[tuple[str, int]]:
    """Test cases for duration parsing (input, expected_seconds)."""
    return [
        ("30s", 30),
        ("5m", 300),
        ("1h", 3600),
        ("60", 60),
        ("", 0),
    ]
