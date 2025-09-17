"""
Tests for compose file validation.
"""

from lazycloud_api.services.compose.models import (
    ComposeFile,
    ComposeService,
    ComposeNetwork,
    ComposeVolumeDefinition,
    ComposeSecretDefinition,
)
from lazycloud_api.services.k8s.validators import ComposeValidator


class TestComposeValidator:
    """Test cases for ComposeValidator."""

    def test_invalid_name_detection(self):
        """Test detection of non-Kubernetes-compliant names."""
        compose = ComposeFile(
            version="3.8",
            services={
                "my-service": ComposeService(image="nginx:latest"),  # Valid
                "my_service": ComposeService(
                    image="nginx:latest"
                ),  # Invalid: underscore
                "MY-SERVICE": ComposeService(
                    image="nginx:latest"
                ),  # Invalid: uppercase
                "123-service": ComposeService(
                    image="apache:latest"
                ),  # Invalid: starts with number
                "-service": ComposeService(
                    image="apache:latest"
                ),  # Invalid: starts with hyphen
                "service-": ComposeService(
                    image="apache:latest"
                ),  # Invalid: ends with hyphen
                "a" * 70: ComposeService(image="apache:latest"),  # Invalid: too long
            },
        )

        validator = ComposeValidator()
        errors, warnings = validator.validate(compose)

        # Should have invalid name errors
        name_errors = [e for e in errors if e.error_type == "invalid_name"]
        assert len(name_errors) == 6  # All except "my-service" are invalid

        # Check specific error messages
        error_messages = [str(e) for e in name_errors]
        assert any("underscore" in msg or "my_service" in msg for msg in error_messages)
        assert any("uppercase" in msg or "MY-SERVICE" in msg for msg in error_messages)
        assert any("cannot start with a number" in msg for msg in error_messages)
        assert any("cannot start with a hyphen" in msg for msg in error_messages)
        assert any("cannot end with a hyphen" in msg for msg in error_messages)
        assert any("63 characters or less" in msg for msg in error_messages)

    def test_multiple_networks_warning(self):
        """Test warning for services with multiple networks."""
        compose = ComposeFile(
            version="3.8",
            services={
                "web": ComposeService(
                    image="nginx:latest",
                    networks=["frontend", "backend"],  # Multiple networks
                )
            },
            networks={"frontend": ComposeNetwork(), "backend": ComposeNetwork()},
        )

        validator = ComposeValidator()
        errors, warnings = validator.validate(compose)

        # Should be a warning now, not an error
        assert len(errors) == 0
        network_warnings = [
            w for w in warnings if w.error_type == "multiple_networks_info"
        ]
        assert len(network_warnings) == 1
        assert "frontend" in str(network_warnings[0])
        assert "backend" in str(network_warnings[0])
        assert "NetworkPolicies" in str(network_warnings[0])

    def test_bind_mount_detection(self):
        """Test detection of bind mounts."""
        compose = ComposeFile(
            version="3.8",
            services={
                "web": ComposeService(
                    image="nginx:latest",
                    volumes=[
                        "./config:/etc/nginx",  # Relative bind mount
                        "/host/path:/container/path",  # Absolute bind mount
                        "named_volume:/data",  # Named volume (OK)
                    ],
                )
            },
            volumes={"named_volume": ComposeVolumeDefinition()},
        )

        validator = ComposeValidator()
        errors, warnings = validator.validate(compose)

        bind_errors = [e for e in errors if e.error_type == "bind_mount"]
        assert len(bind_errors) == 2
        assert any("./config:/etc/nginx" in str(e) for e in bind_errors)
        assert any("/host/path:/container/path" in str(e) for e in bind_errors)

    def test_undefined_volume_detection(self):
        """Test detection of undefined volumes."""
        compose = ComposeFile(
            version="3.8",
            services={
                "db": ComposeService(
                    image="postgres:13",
                    volumes=["undefined_volume:/var/lib/postgresql/data"],
                )
            },
            # No volumes section defining 'undefined_volume'
        )

        validator = ComposeValidator()
        errors, warnings = validator.validate(compose)

        volume_errors = [e for e in errors if e.error_type == "undefined_volume"]
        assert len(volume_errors) == 1
        assert "undefined_volume" in str(volume_errors[0])

    def test_undefined_secret_detection(self):
        """Test detection of undefined secrets."""
        compose = ComposeFile(
            version="3.8",
            services={
                "web": ComposeService(
                    image="nginx:latest", secrets=["api_key", "db_password"]
                )
            },
            secrets={
                "api_key": ComposeSecretDefinition(file="./api_key.txt")
                # db_password is not defined
            },
        )

        validator = ComposeValidator()
        errors, warnings = validator.validate(compose)

        secret_errors = [e for e in errors if e.error_type == "undefined_secret"]
        assert len(secret_errors) == 1
        assert "db_password" in str(secret_errors[0])

    def test_resource_validation(self):
        """Test resource specification validation."""
        compose = ComposeFile(
            version="3.8",
            services={
                "web": ComposeService(
                    image="nginx:latest",
                    deploy={
                        "resources": {
                            "limits": {"cpus": "2.5", "memory": "2G"}
                            # No reservations - should warn
                        }
                    },
                ),
                "bad": ComposeService(
                    image="nginx:latest",
                    deploy={
                        "resources": {
                            "limits": {
                                "cpus": "invalid",  # Invalid CPU
                                "memory": "2TB",  # Valid but unusual
                            }
                        }
                    },
                ),
            },
        )

        validator = ComposeValidator()
        errors, warnings = validator.validate(compose)

        # Should have auto-generated requests warning
        request_warnings = [
            w for w in warnings if w.error_type == "auto_generated_requests"
        ]
        assert len(request_warnings) >= 1

        # Should have invalid CPU error
        cpu_errors = [e for e in errors if e.error_type == "invalid_cpu"]
        assert len(cpu_errors) == 1
        assert "invalid" in str(cpu_errors[0])

    def test_port_conflict_detection(self):
        """Test detection of port conflicts."""
        compose = ComposeFile(
            version="3.8",
            services={
                "web1": ComposeService(image="nginx:latest", ports=["8080:80"]),
                "web2": ComposeService(
                    image="nginx:latest",
                    ports=["8080:80"],  # Same host port
                ),
            },
        )

        validator = ComposeValidator()
        errors, warnings = validator.validate(compose)

        port_errors = [e for e in errors if e.error_type == "port_conflict"]
        assert len(port_errors) == 1
        assert "8080" in str(port_errors[0])
        assert "web1" in str(port_errors[0])

    def test_missing_image(self):
        """Test detection of services without images."""
        # Since ComposeService requires image, we'll test with empty string
        compose = ComposeFile(
            version="3.8",
            services={
                "web": ComposeService(image="")  # Empty image
            },
        )

        validator = ComposeValidator()
        errors, warnings = validator.validate(compose)

        image_errors = [e for e in errors if e.error_type == "missing_image"]
        assert len(image_errors) == 1
        assert "web" in str(image_errors[0])

    def test_external_resources_warnings(self):
        """Test warnings for external resources."""
        compose = ComposeFile(
            version="3.8",
            services={"web": ComposeService(image="nginx:latest")},
            networks={"external-net": ComposeNetwork(external=True)},
            volumes={"external-vol": ComposeVolumeDefinition(external=True)},
            secrets={"external-secret": ComposeSecretDefinition(external=True)},
        )

        validator = ComposeValidator()
        errors, warnings = validator.validate(compose)

        # Should have warnings for external resources
        external_warnings = [w for w in warnings if "external" in w.error_type]
        assert len(external_warnings) == 3

        # Should have no errors
        assert len(errors) == 0

    def test_valid_compose_file(self):
        """Test that a valid compose file produces no errors."""
        compose = ComposeFile(
            version="3.8",
            services={
                "web": ComposeService(
                    image="nginx:latest",
                    ports=["8080:80"],
                    environment={"APP_ENV": "production"},
                    volumes=["app-data:/data"],
                    networks=["frontend"],
                ),
                "db": ComposeService(
                    image="postgres:13",
                    environment={"POSTGRES_PASSWORD": "secret"},
                    volumes=["db-data:/var/lib/postgresql/data"],
                    networks=["frontend"],
                    deploy={
                        "resources": {
                            "limits": {"cpus": "1.0", "memory": "1G"},
                            "reservations": {"cpus": "0.5", "memory": "512M"},
                        }
                    },
                ),
            },
            networks={"frontend": ComposeNetwork()},
            volumes={
                "app-data": ComposeVolumeDefinition(),
                "db-data": ComposeVolumeDefinition(),
            },
        )

        validator = ComposeValidator()
        errors, warnings = validator.validate(compose)

        # Should have no errors
        assert len(errors) == 0

        # May have some warnings but they should be minor
        assert all(w.error_type != "critical" for w in warnings)
