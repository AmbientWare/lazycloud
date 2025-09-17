"""
Tests for security context generation in Kubernetes deployments.
"""

from lazycloud_api.services.k8s.workloads import (
    generate_security_context_values,
    generate_pod_security_context_values,
)
from lazycloud_api.services.compose.models import ComposeFile, ComposeService
from lazycloud_api.services.k8s.helm_values_generator import HelmValuesGenerator


class TestSecurityContextGeneration:
    """Test cases for security context generation."""

    def test_default_security_context(self):
        """Test default security context for generic image."""
        context = generate_security_context_values("myapp:latest")

        assert context["runAsNonRoot"] is True
        assert context["runAsUser"] == 1000
        assert context["runAsGroup"] == 3000
        assert context["readOnlyRootFilesystem"] is False
        assert context["allowPrivilegeEscalation"] is False
        assert context["capabilities"]["drop"] == ["ALL"]
        assert "add" not in context["capabilities"]

    def test_security_context_with_low_port(self):
        """Test security context when service binds to port < 1024."""
        ports = [{"targetPort": 80, "port": 80, "protocol": "TCP"}]
        context = generate_security_context_values("nginx:latest", ports)

        # Should add NET_BIND_SERVICE capability
        assert context["capabilities"]["add"] == ["NET_BIND_SERVICE"]

    def test_security_context_postgres(self):
        """Test security context for PostgreSQL image."""
        context = generate_security_context_values("postgres:13")

        assert context["runAsUser"] == 999
        assert context["runAsGroup"] == 999
        assert context["runAsNonRoot"] is True

    def test_security_context_mysql(self):
        """Test security context for MySQL image."""
        context = generate_security_context_values("mysql:8.0")

        assert context["runAsUser"] == 999
        assert context["runAsGroup"] == 999

    def test_security_context_nginx(self):
        """Test security context for nginx image."""
        context = generate_security_context_values("nginx:alpine")

        assert context["runAsUser"] == 101  # nginx user
        assert context["runAsGroup"] == 101

    def test_pod_security_context_no_volumes(self):
        """Test pod security context without volumes."""
        context = generate_pod_security_context_values(has_volumes=False)

        assert context["runAsNonRoot"] is True
        assert context["seccompProfile"]["type"] == "RuntimeDefault"
        assert "fsGroup" not in context

    def test_pod_security_context_with_volumes(self):
        """Test pod security context with volumes."""
        context = generate_pod_security_context_values(has_volumes=True)

        assert context["runAsNonRoot"] is True
        assert context["seccompProfile"]["type"] == "RuntimeDefault"
        assert context["fsGroup"] == 2000
        assert context["fsGroupChangePolicy"] == "OnRootMismatch"

    def test_helm_values_include_security_contexts(self):
        """Test that Helm values include security contexts."""
        compose = ComposeFile(
            version="3.8",
            services={
                "web": ComposeService(image="nginx:latest", ports=["80:80"]),
                "api": ComposeService(
                    image="node:16", ports=["3000:3000"], volumes=["data:/app/data"]
                ),
            },
            volumes={"data": {}},
        )

        generator = HelmValuesGenerator(namespace="test", user_id="user123")
        helm_values, _ = generator.generate_values(compose)

        # Check web service security contexts
        web_security = helm_values["services"]["web"]["securityContext"]
        assert web_security["runAsUser"] == 101  # nginx user
        assert web_security["capabilities"]["add"] == ["NET_BIND_SERVICE"]  # Port 80

        web_pod_security = helm_values["services"]["web"]["podSecurityContext"]
        assert web_pod_security["runAsNonRoot"] is True
        assert "fsGroup" not in web_pod_security  # No volumes

        # Check API service security contexts
        api_security = helm_values["services"]["api"]["securityContext"]
        assert api_security["runAsUser"] == 1000  # Default non-root
        assert "add" not in api_security["capabilities"]  # Port 3000 > 1024

        api_pod_security = helm_values["services"]["api"]["podSecurityContext"]
        assert api_pod_security["fsGroup"] == 2000  # Has volumes

    def test_security_context_override_in_deployment(self):
        """Test that service-specific security contexts can override defaults."""
        compose = ComposeFile(
            version="3.8",
            services={
                "custom": ComposeService(
                    image="custom:latest",
                    labels={
                        "lazycloud.security.runAsUser": "2000",
                        "lazycloud.security.runAsGroup": "2000",
                    },
                )
            },
        )

        generator = HelmValuesGenerator(namespace="test", user_id="user123")
        helm_values, _ = generator.generate_values(compose)

        # Basic security context should be generated
        assert "securityContext" in helm_values["services"]["custom"]
        assert "podSecurityContext" in helm_values["services"]["custom"]
