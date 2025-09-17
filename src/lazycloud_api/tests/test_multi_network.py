"""
Tests for multi-network service handling with NetworkPolicies.
"""

from lazycloud_api.services.compose.models import (
    ComposeFile,
    ComposeNetwork,
    ComposeService,
)
from lazycloud_api.services.k8s.helm_values_generator import HelmValuesGenerator
from lazycloud_api.services.k8s.validators import ComposeValidator


class TestMultiNetworkHandling:
    """Test cases for multi-network service scenarios."""

    def test_multi_network_generates_warning_not_error(self):
        """Test that multiple networks generate a warning, not an error."""
        compose = ComposeFile(
            version="3.8",
            services={
                "web": ComposeService(
                    image="nginx:latest",
                    networks=["frontend", "backend"],  # Multiple networks
                ),
                "api": ComposeService(
                    image="node:16",
                    networks=["backend"],  # Single network
                ),
                "db": ComposeService(
                    image="postgres:13",
                    networks=["backend", "data"],  # Multiple networks
                ),
            },
            networks={
                "frontend": ComposeNetwork(),
                "backend": ComposeNetwork(),
                "data": ComposeNetwork(),
            },
        )

        validator = ComposeValidator()
        errors, warnings = validator.validate(compose)

        # Should have no errors
        assert len(errors) == 0

        # Should have warnings for services with multiple networks
        multi_network_warnings = [
            w for w in warnings if w.error_type == "multiple_networks_info"
        ]
        assert len(multi_network_warnings) == 2  # web and db

        # Check warning messages
        warning_messages = [str(w) for w in multi_network_warnings]
        assert any(
            "web" in msg and "frontend" in msg and "backend" in msg
            for msg in warning_messages
        )
        assert any(
            "db" in msg and "backend" in msg and "data" in msg
            for msg in warning_messages
        )
        assert all("NetworkPolicies" in msg for msg in warning_messages)

    def test_helm_generation_succeeds_with_multi_network(self):
        """Test that Helm values can be generated for multi-network services."""
        compose = ComposeFile(
            version="3.8",
            services={
                "frontend": ComposeService(
                    image="nginx:latest",
                    ports=["80:80"],
                    networks=["public", "internal"],
                ),
                "backend": ComposeService(
                    image="node:16", ports=["3000:3000"], networks=["internal"]
                ),
                "cache": ComposeService(
                    image="redis:alpine", networks=["internal", "cache-network"]
                ),
            },
            networks={
                "public": ComposeNetwork(),
                "internal": ComposeNetwork(),
                "cache-network": ComposeNetwork(),
            },
        )

        generator = HelmValuesGenerator(user_id="user123")

        # Should not raise an error
        helm_values, warnings = generator.generate_values(compose)

        # Should have generated values
        assert "services" in helm_values
        assert "frontend" in helm_values["services"]
        assert "backend" in helm_values["services"]
        assert "cache" in helm_values["services"]

        # Services should retain their network information
        assert helm_values["services"]["frontend"]["networks"] == ["public", "internal"]
        assert helm_values["services"]["backend"]["networks"] == ["internal"]
        assert helm_values["services"]["cache"]["networks"] == [
            "internal",
            "cache-network",
        ]

        # Should have warnings about multi-network services
        assert len(warnings) > 0
        multi_network_warnings = [
            w for w in warnings if "multiple networks" in w.lower()
        ]
        assert len(multi_network_warnings) > 0

    def test_network_isolation_intent_preserved(self):
        """Test that network isolation intent is preserved in the generated values."""
        compose = ComposeFile(
            version="3.8",
            services={
                "public-web": ComposeService(image="nginx:latest", networks=["public"]),
                "internal-api": ComposeService(image="node:16", networks=["internal"]),
                "shared-db": ComposeService(
                    image="postgres:13", networks=["internal", "data"]
                ),
            },
            networks={
                "public": ComposeNetwork(),
                "internal": ComposeNetwork(),
                "data": ComposeNetwork(),
            },
        )

        generator = HelmValuesGenerator(user_id="user123")
        helm_values, _ = generator.generate_values(compose)

        # Verify services in different networks are isolated
        # public-web should only be in 'public' network
        assert helm_values["services"]["public-web"]["networks"] == ["public"]

        # internal-api should only be in 'internal' network
        assert helm_values["services"]["internal-api"]["networks"] == ["internal"]

        # shared-db should be in both 'internal' and 'data' networks
        assert set(helm_values["services"]["shared-db"]["networks"]) == {
            "internal",
            "data",
        }

        # This network configuration can be used by NetworkPolicies to enforce isolation
