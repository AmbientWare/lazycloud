"""
Integration tests for Docker Compose to Kubernetes conversion.
"""

import pytest
import yaml

from lazycloud_api.services.compose.parser import ComposeParser
from lazycloud_api.services.k8s.helm_values_generator import HelmValuesGenerator


class TestIntegration:
    """Integration test cases for the complete pipeline."""

    @pytest.fixture
    def comprehensive_compose_dict(self):
        """A comprehensive compose configuration for testing."""
        return {
            "version": "3.8",
            "services": {
                "api": {
                    "image": "myapp:latest",
                    "command": ["uvicorn", "main:app", "--host", "0.0.0.0"],
                    "ports": ["8080:8080", "9090:9090"],
                    "environment": {
                        "LOG_LEVEL": "info",
                        "DATABASE_URL": "postgresql://postgres:5432/myapp",
                    },
                    "volumes": ["api-config:/config"],
                    "deploy": {
                        "replicas": 3,
                        "resources": {
                            "limits": {"cpus": "2", "memory": "2Gi"},
                            "reservations": {"cpus": "1", "memory": "1Gi"},
                        },
                        "restart_policy": {"condition": "always"},
                    },
                    "healthcheck": {
                        "test": ["CMD", "curl", "-f", "http://localhost:8080/health"],
                        "interval": "30s",
                        "timeout": "5s",
                        "retries": 3,
                    },
                    "labels": {
                        "lazycloud.enable_ingress": "true",
                        "lazycloud.hostname_prefix": "api",
                        "lazycloud.scaling.enabled": "enabled",
                        "lazycloud.scaling.min": "2",
                        "lazycloud.scaling.max": "20",
                        "lazycloud.scaling.cpu": "75",
                        "lazycloud.metrics": "true",
                        "lazycloud.metrics.port": "9090",
                        "lazycloud.config_files": "app.yaml:/config/app.yaml",
                    },
                },
                "postgres": {
                    "image": "postgres:13",
                    "ports": ["5432:5432"],
                    "volumes": ["pg-data:/var/lib/postgresql/data"],
                    "environment": {
                        "POSTGRES_DB": "myapp",
                        "POSTGRES_USER": "user",
                        "POSTGRES_PASSWORD": "password",
                    },
                    "deploy": {
                        "resources": {
                            "limits": {"cpus": "4", "memory": "8Gi"},
                            "reservations": {"cpus": "2", "memory": "4Gi"},
                        }
                    },
                },
                "redis": {
                    "image": "redis:alpine",
                    "ports": ["6379:6379"],
                    "volumes": ["redis-data:/data"],
                },
                "frontend": {
                    "image": "nginx:latest",
                    "ports": ["80:80"],
                    "labels": {
                        "lazycloud.enable_ingress": "true",
                        "lazycloud.scaling.enabled": "true",
                    },
                },
            },
            "volumes": {"pg-data": {}, "redis-data": {}, "api-config": {}},
            "networks": {"app-network": {"driver": "bridge"}},
        }

    def test_complete_pipeline(self, comprehensive_compose_dict):
        """Test the complete Docker Compose to Helm values pipeline."""
        parser = ComposeParser()
        generator = HelmValuesGenerator("test-user")

        # Parse compose
        compose_file = parser.parse_dict(comprehensive_compose_dict)

        # Generate Helm values
        helm_values, warnings = generator.generate_values(compose_file)

        # Validate structure
        assert "global" in helm_values
        assert "services" in helm_values
        assert "volumes" in helm_values
        assert "networks" in helm_values

        assert len(helm_values["services"]) == 4
        assert len(helm_values["volumes"]) == 3

    def test_api_service_complete_configuration(self, comprehensive_compose_dict):
        """Test API service gets all expected configurations."""
        parser = ComposeParser()
        generator = HelmValuesGenerator("test-user")

        compose_file = parser.parse_dict(comprehensive_compose_dict)
        helm_values, warnings = generator.generate_values(compose_file)

        api = helm_values["services"]["api"]

        # Basic service config
        assert api["enabled"] is True
        assert api["image"]["repository"] == "myapp"
        assert api["image"]["tag"] == "latest"
        assert api["image"]["pullPolicy"] == "Always"
        assert api["command"] == ["uvicorn", "main:app", "--host", "0.0.0.0"]

        # Environment
        assert api["environment"]["LOG_LEVEL"] == "info"
        assert api["environment"]["DATABASE_URL"] == "postgresql://postgres:5432/myapp"

        # Ports
        assert len(api["ports"]) == 2
        assert api["ports"][0]["port"] == 8080
        assert api["ports"][1]["port"] == 9090

        # Volumes
        assert len(api["volumes"]) == 1
        assert api["volumes"][0]["name"] == "api-config"
        assert api["volumes"][0]["mountPath"] == "/config"

        # Resources
        assert api["resources"]["limits"]["cpu"] == "2"
        assert api["resources"]["limits"]["memory"] == "2Gi"
        assert api["resources"]["requests"]["cpu"] == "1"
        assert api["resources"]["requests"]["memory"] == "1Gi"

        # Replicas and restart policy
        assert api["replicas"] == 3
        assert api["restartPolicy"] == "Always"

        # Health checks
        assert api["healthcheck"]["enabled"] is True
        assert "livenessProbe" in api["healthcheck"]
        assert "readinessProbe" in api["healthcheck"]

        # Ingress
        assert api["ingress"]["enabled"] is True
        assert api["ingress"]["hostnamePrefix"] == "api"
        assert api["ingress"]["tls"]["enabled"] is True

        # HPA
        assert api["hpa"]["enabled"] is True
        assert api["hpa"]["minReplicas"] == 2
        assert api["hpa"]["maxReplicas"] == 20

        # ConfigMaps
        assert api["configMaps"]["enabled"] is True
        assert len(api["configMaps"]["configs"]) == 1

        # Metrics
        assert api["metrics"]["enabled"] is True
        assert api["metrics"]["port"] == "9090"

    def test_deployment_generation_integration(self, comprehensive_compose_dict):
        """Test Deployment generation in integration."""
        parser = ComposeParser()
        generator = HelmValuesGenerator("test-user")

        compose_file = parser.parse_dict(comprehensive_compose_dict)
        helm_values, warnings = generator.generate_values(compose_file)

        # All services should be Deployments
        postgres = helm_values["services"]["postgres"]
        assert postgres["workloadType"] == "Deployment"

        redis = helm_values["services"]["redis"]
        assert redis["workloadType"] == "Deployment"

        # API and frontend should be Deployments
        api = helm_values["services"]["api"]
        frontend = helm_values["services"]["frontend"]
        assert "workloadType" not in api
        assert "workloadType" not in frontend

    def test_ingress_petname_generation(self, comprehensive_compose_dict):
        """Test that frontend gets auto-generated petname."""
        parser = ComposeParser()
        generator = HelmValuesGenerator("test-user")

        compose_file = parser.parse_dict(comprehensive_compose_dict)
        helm_values, warnings = generator.generate_values(compose_file)

        frontend = helm_values["services"]["frontend"]
        assert frontend["ingress"]["enabled"] is True

        # Should have auto-generated petname
        petname = frontend["ingress"]["hostnamePrefix"]
        assert petname != ""
        assert "-" in petname  # Should be in "adjective-animal" format

    def test_volume_management_integration(self, comprehensive_compose_dict):
        """Test volume management across the pipeline."""
        parser = ComposeParser()
        generator = HelmValuesGenerator("test-user")

        compose_file = parser.parse_dict(comprehensive_compose_dict)
        helm_values, warnings = generator.generate_values(compose_file)

        # Check global volumes
        volumes = helm_values["volumes"]
        expected_volumes = {"pg-data", "redis-data", "api-config"}
        assert set(volumes.keys()) == expected_volumes

        for volume_name, volume_config in volumes.items():
            assert volume_config["enabled"] is True
            assert volume_config["size"] == "1Gi"
            assert volume_config["accessModes"] == ["ReadWriteOnce"]

        # Check service volume mounts
        postgres = helm_values["services"]["postgres"]
        assert len(postgres["volumes"]) == 1
        assert postgres["volumes"][0]["name"] == "pg-data"

        redis = helm_values["services"]["redis"]
        assert len(redis["volumes"]) == 1
        assert redis["volumes"][0]["name"] == "redis-data"

    def test_validation_warnings_integration(self):
        """Test validation warnings generation."""
        parser = ComposeParser()
        generator = HelmValuesGenerator("test-user")

        # Compose with configurations that generate warnings (not errors)
        compose_dict = {
            "version": "3.8",
            "services": {
                "web": {
                    "image": "myapp",  # Local image - should warn
                    "networks": ["net1", "net2"],  # Multiple networks - should warn
                    "deploy": {
                        "resources": {
                            "limits": {"cpus": "2.0", "memory": "2G"}
                            # No reservations - should warn
                        }
                    },
                }
            },
            "volumes": {
                "external-vol": {"external": True}  # External volume - should warn
            },
            "networks": {
                "net1": {},
                "net2": {},
                "external-net": {"external": True},  # External network - should warn
            },
        }

        compose_file = parser.parse_dict(compose_dict)
        helm_values, warnings = generator.generate_values(compose_file)

        # Should have multiple warnings
        # Should have multiple warnings
        assert (
            len(warnings) >= 4
        )  # Local image, multiple networks, no requests, external resources

        warning_texts = " ".join(warnings)
        assert "local image" in warning_texts.lower()
        assert "external" in warning_texts.lower()
        assert "multiple networks" in warning_texts.lower()
        assert "auto-generated" in warning_texts.lower()  # For resource requests

    def test_yaml_serialization(self, comprehensive_compose_dict):
        """Test that generated Helm values can be serialized to YAML."""
        parser = ComposeParser()
        generator = HelmValuesGenerator("test-user")

        compose_file = parser.parse_dict(comprehensive_compose_dict)
        helm_values, warnings = generator.generate_values(compose_file)

        # Should be able to serialize to YAML without errors
        yaml_output = yaml.dump(helm_values, default_flow_style=False)
        assert len(yaml_output) > 0

        # Should be able to deserialize back
        parsed_yaml = yaml.safe_load(yaml_output)
        assert parsed_yaml["global"]["userId"] == "test-user"
        assert parsed_yaml["global"]["managedBy"] == "lazycloud"
        assert len(parsed_yaml["services"]) == 4

    def test_empty_compose_file(self):
        """Test handling of minimal compose file."""
        parser = ComposeParser()
        generator = HelmValuesGenerator("test-user")

        minimal_compose = {
            "version": "3.8",
            "services": {"web": {"image": "nginx:latest"}},
        }

        compose_file = parser.parse_dict(minimal_compose)
        helm_values, warnings = generator.generate_values(compose_file)

        assert len(helm_values["services"]) == 1
        web = helm_values["services"]["web"]
        assert web["enabled"] is True
        assert web["image"]["repository"] == "nginx"
        assert web["image"]["tag"] == "latest"

        # Deployments must have restartPolicy: Always (Kubernetes requirement)
        assert web["restartPolicy"] == "Always"
        assert "workloadType" not in web  # Should be Deployment
