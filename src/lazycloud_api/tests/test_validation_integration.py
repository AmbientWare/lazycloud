"""
Integration tests showing validation preventing bad deployments.
"""

import pytest

from lazycloud_api.services.compose.models import ComposeFile, ComposeService
from lazycloud_api.services.k8s.helm_values_generator import HelmValuesGenerator


class TestValidationIntegration:
    """Test validation integration with helm generation."""

    def test_invalid_names_prevent_generation(self):
        """Test that invalid names prevent helm values generation."""
        compose = ComposeFile(
            version="3.8",
            services={
                "web-app": ComposeService(image="nginx:latest"),  # Valid
                "web_app": ComposeService(image="apache:latest"),  # Invalid: underscore
                "WebAPI": ComposeService(image="node:latest"),  # Invalid: uppercase
            },
        )

        generator = HelmValuesGenerator(user_id="user123")

        with pytest.raises(ValueError) as exc_info:
            generator.generate_values(compose)

        error_message = str(exc_info.value)
        assert "validation failed" in error_message.lower()
        assert "not Kubernetes-compliant" in error_message
        assert "web_app" in error_message
        assert "WebAPI" in error_message
        assert any(
            phrase in error_message
            for phrase in ["underscore", "uppercase", "lowercase"]
        )

    def test_multiple_validation_errors(self):
        """Test multiple validation errors are all reported."""
        compose = ComposeFile(
            version="3.8",
            services={
                "web": ComposeService(
                    image="nginx:latest",
                    networks=["frontend"],
                    volumes=["./config:/etc/nginx"],  # Bind mount
                    ports=["8080:80"],
                ),
                "api": ComposeService(
                    image="",  # Missing image
                    ports=["8080:80"],  # Port conflict
                    secrets=["undefined_secret"],  # Undefined secret
                ),
            },
        )

        generator = HelmValuesGenerator(user_id="user123")

        with pytest.raises(ValueError) as exc_info:
            generator.generate_values(compose)

        error_message = str(exc_info.value)

        # Should report all errors
        assert "Bind mount" in error_message
        assert "No image specified" in error_message
        assert "Port 8080 already used" in error_message
        assert "undefined_secret" in error_message

        # Should show the count
        assert "4 error(s)" in error_message

    def test_valid_compose_generates_successfully(self):
        """Test that valid compose files generate successfully with warnings."""
        compose = ComposeFile(
            version="3.8",
            services={
                "web": ComposeService(
                    image="nginx:latest",
                    ports=["8080:80"],
                    environment={"APP_ENV": "production"},
                ),
                "api": ComposeService(  # Lowercase name is valid
                    image="node:16",
                    ports=["3000:3000"],
                    deploy={
                        "resources": {
                            "limits": {"cpus": "1.0", "memory": "512M"}
                            # No reservations - will generate warning
                        }
                    },
                ),
            },
        )

        generator = HelmValuesGenerator(user_id="user123")

        # Should not raise an error
        helm_values, warnings = generator.generate_values(compose)

        # Should have generated values
        assert "services" in helm_values
        assert "web" in helm_values["services"]
        assert "api" in helm_values["services"]

        # Should have warnings about missing resource requests
        assert len(warnings) > 0

        # Check for specific warnings
        warning_text = "\n".join(warnings)
        # Should have warning about missing requests or reservations
        assert any(w for w in ["requests", "reservations"] if w in warning_text)

    def test_suggestions_provided(self):
        """Test that helpful suggestions are provided with errors."""
        compose = ComposeFile(
            version="3.8",
            services={
                "db": ComposeService(
                    image="postgres:13",
                    volumes=[
                        "/host/data:/var/lib/postgresql/data",  # Bind mount
                        "db_logs:/logs",  # Undefined volume
                    ],
                )
            },
        )

        generator = HelmValuesGenerator(user_id="user123")

        with pytest.raises(ValueError) as exc_info:
            generator.generate_values(compose)

        error_message = str(exc_info.value)

        # Should include suggestions
        assert "Suggestion:" in error_message
        assert "named volumes" in error_message or "ConfigMaps" in error_message
        assert "volumes section" in error_message
