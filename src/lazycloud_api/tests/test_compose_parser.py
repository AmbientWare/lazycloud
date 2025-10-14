"""
Tests for Docker Compose parser functionality.
"""

import pytest
from pydantic import ValidationError

from shared.models.compose import ComposeFile


class TestComposeParser:
    """Test cases for ComposeParser."""

    def test_parse_simple_compose_dict(self, compose_parser):
        """Test parsing a simple compose dictionary."""
        compose_dict = {
            "version": "3.8",
            "services": {"web": {"image": "nginx:latest", "ports": ["80:80"]}},
        }

        result = compose_parser.parse_dict(compose_dict)

        assert isinstance(result, ComposeFile)
        assert result.version == "3.8"
        assert "web" in result.services
        assert result.services["web"].image == "nginx:latest"
        assert result.services["web"].ports == ["80:80"]

    def test_parse_compose_file(self, compose_parser, test_compose_file):
        """Test parsing a compose file from disk."""
        result = compose_parser.parse_file(test_compose_file)

        assert isinstance(result, ComposeFile)
        assert "web" in result.services
        assert "db" in result.services
        assert result.volumes is not None
        assert "db-data" in result.volumes

    def test_unsupported_compose_version(self, compose_parser):
        """Test that unsupported compose versions raise error."""
        compose_dict = {
            "version": "2.4",
            "services": {"web": {"image": "nginx:latest"}},
        }

        with pytest.raises(ValueError, match="Unsupported compose version"):
            compose_parser.parse_dict(compose_dict)

    def test_parse_complex_service_config(self, compose_parser):
        """Test parsing complex service configuration."""
        compose_dict = {
            "version": "3.8",
            "services": {
                "api": {
                    "image": "myapp:latest",
                    "command": ["python", "app.py"],
                    "ports": [
                        {"target": 8080, "published": 8080, "protocol": "tcp"},
                        "9090:9090",
                    ],
                    "environment": {"NODE_ENV": "production", "DEBUG": "false"},
                    "volumes": [
                        {"type": "volume", "source": "app-data", "target": "/data"},
                        "config:/config",
                    ],
                    "deploy": {
                        "replicas": 3,
                        "resources": {"limits": {"cpus": "1", "memory": "1Gi"}},
                    },
                    "healthcheck": {
                        "test": ["CMD", "curl", "-f", "http://localhost:8080/health"],
                        "interval": "30s",
                    },
                    "labels": {"lazycloud.enable_ingress": "true"},
                }
            },
            "volumes": {"app-data": {}, "config": {}},
        }

        result = compose_parser.parse_dict(compose_dict)
        service = result.services["api"]

        assert service.image == "myapp:latest"
        assert service.command == ["python", "app.py"]
        assert len(service.ports) == 2
        assert service.environment["NODE_ENV"] == "production"
        assert len(service.volumes) == 2
        assert service.deploy["replicas"] == 3
        assert service.healthcheck is not None
        assert service.labels["lazycloud.enable_ingress"] == "true"

    def test_protocol_validation(self, compose_parser):
        """Test that invalid protocols are rejected."""
        compose_dict = {
            "version": "3.8",
            "services": {
                "web": {
                    "image": "nginx:latest",
                    "ports": [
                        {"target": 80, "published": 80, "protocol": "rtp"}  # Invalid
                    ],
                }
            },
        }

        with pytest.raises(ValidationError, match="Invalid protocol"):
            compose_parser.parse_dict(compose_dict)

    def test_extension_fields_support(self, compose_parser):
        """Test that x- extension fields work correctly."""
        compose_dict = {
            "version": "3.8",
            "x-common-env": {"LOG_LEVEL": "info", "TZ": "UTC"},
            "services": {
                "web": {
                    "image": "nginx:latest",
                    "environment": {
                        "LOG_LEVEL": "info",
                        "TZ": "UTC",
                        "SERVICE_NAME": "web",
                    },
                }
            },
        }

        result = compose_parser.parse_dict(compose_dict)
        service = result.services["web"]

        # Extension fields should be ignored by parser, but environment should work
        assert service.environment["LOG_LEVEL"] == "info"
        assert service.environment["SERVICE_NAME"] == "web"

    def test_missing_required_fields(self, compose_parser):
        """Test behavior with missing required fields."""
        compose_dict = {
            "version": "3.8",
            "services": {
                "web": {
                    # Missing 'image' field is now optional (for extends pattern)
                    "ports": ["80:80"]
                }
            },
        }

        # The parser now allows missing image field
        result = compose_parser.parse_dict(compose_dict)
        assert result.services["web"].image is None

    def test_file_not_found(self, compose_parser):
        """Test FileNotFoundError for non-existent files."""
        with pytest.raises(FileNotFoundError):
            compose_parser.parse_file("non-existent-file.yml")

    def test_skip_service_with_label(self, compose_parser):
        """Test that services with lazycloud.skip: 'true' are excluded."""
        compose_dict = {
            "version": "3.8",
            "services": {
                "web": {"image": "nginx:latest", "ports": ["80:80"]},
                "local-db": {
                    "image": "postgres:17",
                    "labels": {"lazycloud.skip": "true"},
                    "ports": ["5432:5432"],
                },
            },
        }

        result = compose_parser.parse_dict(compose_dict)

        assert isinstance(result, ComposeFile)
        assert len(result.services) == 1
        assert result.services[0].name == "web"
        # local-db should be skipped
        service_names = [s.name for s in result.services]
        assert "local-db" not in service_names

    def test_skip_service_case_insensitive(self, compose_parser):
        """Test that lazycloud.skip works with different case variations."""
        compose_dict = {
            "version": "3.8",
            "services": {
                "web": {"image": "nginx:latest"},
                "db1": {
                    "image": "postgres:17",
                    "labels": {"lazycloud.skip": "True"},  # Uppercase
                },
                "db2": {
                    "image": "postgres:17",
                    "labels": {"lazycloud.skip": "TRUE"},  # All caps
                },
            },
        }

        result = compose_parser.parse_dict(compose_dict)

        assert len(result.services) == 1
        assert result.services[0].name == "web"

    def test_skip_service_false_includes_service(self, compose_parser):
        """Test that lazycloud.skip: 'false' includes the service."""
        compose_dict = {
            "version": "3.8",
            "services": {
                "web": {"image": "nginx:latest"},
                "api": {
                    "image": "myapi:latest",
                    "labels": {"lazycloud.skip": "false"},
                },
            },
        }

        result = compose_parser.parse_dict(compose_dict)

        assert len(result.services) == 2
        service_names = [s.name for s in result.services]
        assert "web" in service_names
        assert "api" in service_names

    def test_skip_service_no_label_includes_service(self, compose_parser):
        """Test that services without lazycloud.skip label are included."""
        compose_dict = {
            "version": "3.8",
            "services": {
                "web": {"image": "nginx:latest"},
                "api": {"image": "myapi:latest", "labels": {"other.label": "value"}},
            },
        }

        result = compose_parser.parse_dict(compose_dict)

        assert len(result.services) == 2
        service_names = [s.name for s in result.services]
        assert "web" in service_names
        assert "api" in service_names

    def test_skip_service_filters_unused_volumes(self, compose_parser):
        """Test that volumes only used by skipped services are excluded."""
        compose_dict = {
            "version": "3.8",
            "services": {
                "web": {
                    "image": "nginx:latest",
                    "volumes": ["web-data:/data"],
                },
                "local-db": {
                    "image": "postgres:17",
                    "labels": {"lazycloud.skip": "true"},
                    "volumes": ["db-data:/var/lib/postgresql/data"],
                },
            },
            "volumes": {
                "web-data": {},
                "db-data": {},
            },
        }

        result = compose_parser.parse_dict(compose_dict)

        # Only web service should be included
        assert len(result.services) == 1
        assert result.services[0].name == "web"

        # Only web-data volume should be included
        assert len(result.volumes) == 1
        assert result.volumes[0].name == "web-data"

    def test_skip_service_filters_unused_networks(self, compose_parser):
        """Test that networks only used by skipped services are excluded."""
        compose_dict = {
            "version": "3.8",
            "services": {
                "web": {
                    "image": "nginx:latest",
                    "networks": ["frontend"],
                },
                "local-db": {
                    "image": "postgres:17",
                    "labels": {"lazycloud.skip": "true"},
                    "networks": ["backend"],
                },
            },
            "networks": {
                "frontend": {},
                "backend": {},
            },
        }

        result = compose_parser.parse_dict(compose_dict)

        # Only web service should be included
        assert len(result.services) == 1
        assert result.services[0].name == "web"

        # Only frontend network should be included
        assert len(result.networks) == 1
        assert result.networks[0].name == "frontend"

    def test_skip_service_keeps_shared_resources(self, compose_parser):
        """Test that networks/volumes shared by non-skipped services are kept."""
        compose_dict = {
            "version": "3.8",
            "services": {
                "web": {
                    "image": "nginx:latest",
                    "networks": ["shared"],
                    "volumes": ["shared-data:/data"],
                },
                "api": {
                    "image": "myapi:latest",
                    "networks": ["shared"],
                    "volumes": ["shared-data:/app/data"],
                },
                "local-db": {
                    "image": "postgres:17",
                    "labels": {"lazycloud.skip": "true"},
                    "networks": ["shared"],
                    "volumes": ["shared-data:/var/lib/postgresql"],
                },
            },
            "networks": {
                "shared": {},
            },
            "volumes": {
                "shared-data": {},
            },
        }

        result = compose_parser.parse_dict(compose_dict)

        # web and api should be included, local-db skipped
        assert len(result.services) == 2
        service_names = [s.name for s in result.services]
        assert "web" in service_names
        assert "api" in service_names

        # shared network and volume should still be included
        assert len(result.networks) == 1
        assert result.networks[0].name == "shared"
        assert len(result.volumes) == 1
        assert result.volumes[0].name == "shared-data"
