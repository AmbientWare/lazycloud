"""
Pytest configuration and fixtures for LazyCloud API tests.
"""

import pytest

from lazycloud_api.services.compose.parser import ComposeParser
from lazycloud_api.services.k8s.helm_values_generator import HelmValuesGenerator


@pytest.fixture
def compose_parser():
    """Create a ComposeParser instance."""
    return ComposeParser()


@pytest.fixture
def helm_generator():
    """Create a HelmValuesGenerator instance."""
    return HelmValuesGenerator("test-user")


@pytest.fixture
def sample_compose_data():
    """Sample compose data for testing."""
    return {
        "version": "3.8",
        "services": {
            "api": {
                "image": "myapp:latest",
                "ports": ["8080:8080"],
                "environment": {"NODE_ENV": "production"},
                "deploy": {
                    "replicas": 3,
                    "resources": {
                        "limits": {"cpus": "1", "memory": "1Gi"},
                        "reservations": {"cpus": "0.5", "memory": "512Mi"},
                    },
                },
                "labels": {
                    "lazycloud.enable_ingress": "true",
                    "lazycloud.hostname_prefix": "api",
                    "lazycloud.scaling.enabled": "enabled",
                    "lazycloud.scaling.min": "2",
                    "lazycloud.scaling.max": "10",
                },
            },
            "postgres": {
                "image": "postgres:13",
                "ports": ["5432:5432"],
                "volumes": ["pg-data:/var/lib/postgresql/data"],
                "environment": {"POSTGRES_DB": "myapp"},
            },
        },
        "volumes": {"pg-data": {}},
    }


@pytest.fixture
def test_compose_file(tmp_path):
    """Create a temporary compose file for testing."""
    compose_content = """
version: '3.8'
services:
  web:
    image: nginx:latest
    ports:
      - "80:80"
    labels:
      lazycloud.enable_ingress: "true"
      lazycloud.metrics: "true"
  
  db:
    image: postgres:13
    volumes:
      - db-data:/var/lib/postgresql/data
    environment:
      POSTGRES_DB: test

volumes:
  db-data:
"""

    compose_file = tmp_path / "docker-compose.yml"
    compose_file.write_text(compose_content)
    return compose_file
