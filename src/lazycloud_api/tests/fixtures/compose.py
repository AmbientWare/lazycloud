"""Compose test fixtures - data dicts and ComposeFile objects."""

from typing import Any

import pytest

from lazycloud_api.tests.fixtures.compose_cases import (
    ALL_NEW_FIELDS,
    BASIC_SERVICE,
    COMMAND_LIST_ONLY,
    COMMAND_ONLY,
    COMPLEX_MULTI_SERVICE,
    ENTRYPOINT_AND_COMMAND,
    ENTRYPOINT_ONLY,
    ENTRYPOINT_STRING,
    SERVICE_WITH_DOMAIN,
    SINGLE_SERVICE_CASES,
    STOP_GRACE_PERIOD_COMBINED,
    STOP_GRACE_PERIOD_MINUTES,
    STOP_GRACE_PERIOD_SECONDS,
    WORKING_DIR,
)
from shared.models.compose import (
    ComposeFile,
    ComposeNetwork,
    ComposePort,
    ComposeService,
    ComposeVolume,
    DeployConfig,
    HealthCheck,
    ResourceConfig,
    ResourcesConfig,
    ServiceNetwork,
    ServiceVolume,
)

# ============================================================================
# Compose Data (dict) - for parser tests
# ============================================================================


@pytest.fixture
def simple_compose_data() -> dict[str, Any]:
    """Simple compose data with one service."""
    return {
        "version": "3.8",
        "services": {
            "web": {
                "image": "nginx:latest",
                "ports": ["80:80"],
            }
        },
    }


@pytest.fixture
def complex_compose_data() -> dict[str, Any]:
    """Complex compose data with multiple services, volumes, networks."""
    return {
        "version": "3.8",
        "services": {
            "api": {
                "image": "myapp:latest",
                "ports": ["8080:8080"],
                "environment": {"NODE_ENV": "production"},
                "volumes": ["app-data:/data"],
                "networks": ["backend"],
                "deploy": {
                    "replicas": 3,
                    "resources": {
                        "limits": {"cpus": "1", "memory": "1G"},
                        "reservations": {"cpus": "0.5", "memory": "512M"},
                    },
                },
                "healthcheck": {
                    "test": ["CMD", "curl", "-f", "http://localhost:8080/health"],
                    "interval": "30s",
                    "timeout": "10s",
                    "retries": 3,
                },
            },
            "postgres": {
                "image": "postgres:15",
                "ports": ["5432:5432"],
                "volumes": ["pg-data:/var/lib/postgresql/data"],
                "networks": ["backend"],
                "environment": {"POSTGRES_DB": "myapp"},
            },
            "redis": {
                "image": "redis:7",
                "ports": ["6379:6379"],
                "networks": ["backend"],
            },
        },
        "volumes": {
            "app-data": {},
            "pg-data": {},
        },
        "networks": ["backend"],
    }


@pytest.fixture
def scaling_compose_data() -> dict[str, Any]:
    """Compose data with scaling configuration."""
    return {
        "services": {
            "api": {
                "image": "myapp:latest",
                "ports": ["8080:8080"],
                "deploy": {
                    "replicas": 2,
                    "labels": {
                        "lazycloud.scaling.enabled": "true",
                        "lazycloud.scaling.min": "2",
                        "lazycloud.scaling.max": "10",
                        "lazycloud.scaling.cpu": "0.7",
                        "lazycloud.scaling.memory": "0.8",
                    },
                },
            }
        },
    }


# ============================================================================
# ComposeFile Objects - for validator, diff checker, helm generator tests
# ============================================================================


@pytest.fixture
def simple_compose_file() -> ComposeFile:
    """Simple parsed compose file."""
    return ComposeFile(
        version="3.8",
        services=[
            ComposeService(
                name="web",
                image="nginx:latest",
                ports=[ComposePort(published=80, target=80, protocol="tcp")],
            )
        ],
    )


@pytest.fixture
def complex_compose_file() -> ComposeFile:
    """Complex parsed compose file."""
    return ComposeFile(
        version="3.8",
        services=[
            ComposeService(
                name="api",
                image="myapp:latest",
                ports=[ComposePort(published=8080, target=8080, protocol="tcp")],
                volumes=[
                    ServiceVolume(type="volume", source="app-data", target="/data")
                ],
                networks=[ServiceNetwork(name="backend")],
                deploy=DeployConfig(
                    replicas=3,
                    resources=ResourcesConfig(
                        limits=ResourceConfig(cpus="1", memory="1Gi"),
                        reservations=ResourceConfig(cpus="0.5", memory="512Mi"),
                    ),
                ),
                healthcheck=HealthCheck(
                    test=["CMD", "curl", "-f", "http://localhost:8080/health"],
                    interval="30s",
                    timeout="10s",
                    retries=3,
                ),
            ),
            ComposeService(
                name="postgres",
                image="postgres:15",
                ports=[ComposePort(published=5432, target=5432, protocol="tcp")],
                volumes=[
                    ServiceVolume(
                        type="volume",
                        source="pg-data",
                        target="/var/lib/postgresql/data",
                    )
                ],
                networks=[ServiceNetwork(name="backend")],
            ),
            ComposeService(
                name="redis",
                image="redis:7",
                ports=[ComposePort(published=6379, target=6379, protocol="tcp")],
                networks=[ServiceNetwork(name="backend")],
            ),
        ],
        volumes=[
            ComposeVolume(name="app-data"),
            ComposeVolume(name="pg-data"),
        ],
        networks=[ComposeNetwork(name="backend")],
    )


@pytest.fixture
def shared_volume_compose_file() -> ComposeFile:
    """Compose file where a volume is used by multiple services."""
    return ComposeFile(
        version="3.8",
        services=[
            ComposeService(
                name="api",
                image="myapp:latest",
                volumes=[
                    ServiceVolume(type="volume", source="shared-data", target="/data")
                ],
            ),
            ComposeService(
                name="worker",
                image="myapp-worker:latest",
                volumes=[
                    ServiceVolume(type="volume", source="shared-data", target="/data")
                ],
            ),
        ],
        volumes=[ComposeVolume(name="shared-data")],
    )


# ============================================================================
# Diff Checker Fixtures
# ============================================================================


@pytest.fixture
def current_compose_file() -> ComposeFile:
    """Current compose file for diff testing."""
    return ComposeFile(
        version="3.8",
        services=[
            ComposeService(
                name="web",
                image="nginx:1.20",
                ports=[ComposePort(published=80, target=80, protocol="tcp")],
            ),
            ComposeService(
                name="api",
                image="myapp:v1",
                ports=[ComposePort(published=8080, target=8080, protocol="tcp")],
            ),
        ],
        volumes=[ComposeVolume(name="data")],
    )


@pytest.fixture
def new_compose_file() -> ComposeFile:
    """New compose file for diff testing (with changes)."""
    return ComposeFile(
        version="3.8",
        services=[
            ComposeService(
                name="web",
                image="nginx:1.21",
                ports=[ComposePort(published=80, target=80, protocol="tcp")],
            ),
            ComposeService(
                name="worker",
                image="myapp-worker:v1",
            ),
        ],
        volumes=[
            ComposeVolume(name="data"),
            ComposeVolume(name="cache"),
        ],
    )


# ============================================================================
# Validation Fixtures
# ============================================================================


@pytest.fixture
def valid_service_names() -> list[str]:
    """List of valid Kubernetes service names."""
    return [
        "web",
        "api",
        "my-service",
        "service-123",
        "a",
        "a" * 63,
        "a1b2c3",
    ]


# ============================================================================
# Shared Test Cases (for parametrized testing)
# ============================================================================


@pytest.fixture
def basic_service_case() -> dict[str, Any]:
    """Basic service test case."""
    return BASIC_SERVICE


@pytest.fixture
def entrypoint_only_case() -> dict[str, Any]:
    """Entrypoint only test case."""
    return ENTRYPOINT_ONLY


@pytest.fixture
def entrypoint_string_case() -> dict[str, Any]:
    """Entrypoint as string test case."""
    return ENTRYPOINT_STRING


@pytest.fixture
def command_only_case() -> dict[str, Any]:
    """Command only test case."""
    return COMMAND_ONLY


@pytest.fixture
def command_list_only_case() -> dict[str, Any]:
    """Command as list only test case."""
    return COMMAND_LIST_ONLY


@pytest.fixture
def entrypoint_and_command_case() -> dict[str, Any]:
    """Entrypoint and command test case."""
    return ENTRYPOINT_AND_COMMAND


@pytest.fixture
def working_dir_case() -> dict[str, Any]:
    """Working dir test case."""
    return WORKING_DIR


@pytest.fixture
def stop_grace_period_seconds_case() -> dict[str, Any]:
    """Stop grace period (seconds) test case."""
    return STOP_GRACE_PERIOD_SECONDS


@pytest.fixture
def stop_grace_period_minutes_case() -> dict[str, Any]:
    """Stop grace period (minutes) test case."""
    return STOP_GRACE_PERIOD_MINUTES


@pytest.fixture
def stop_grace_period_combined_case() -> dict[str, Any]:
    """Stop grace period (combined format) test case."""
    return STOP_GRACE_PERIOD_COMBINED


@pytest.fixture
def all_new_fields_case() -> dict[str, Any]:
    """All new fields combined test case."""
    return ALL_NEW_FIELDS


@pytest.fixture
def complex_multi_service_case() -> dict[str, Any]:
    """Complex multi-service test case."""
    return COMPLEX_MULTI_SERVICE


@pytest.fixture
def service_with_domain_case() -> dict[str, Any]:
    """Service with domain label test case."""
    return SERVICE_WITH_DOMAIN


@pytest.fixture
def single_service_cases() -> list[dict[str, Any]]:
    """All single-service test cases for parametrized testing."""
    return SINGLE_SERVICE_CASES
