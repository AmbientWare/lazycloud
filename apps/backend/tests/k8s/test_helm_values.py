"""Tests for HelmValuesGenerator."""

from typing import Any

import pytest
from backend.services.compose.parser import ComposeParser
from backend.services.k8s.helm_values_generator import HelmValuesGenerator
from models.billing import STORAGE_CLASS_EBS, STORAGE_CLASS_EFS
from models.compose import (
    ComposeFile,
    ComposeNetwork,
    ComposePort,
    ComposeService,
    ComposeVolume,
    DeployConfig,
    ResourceConfig,
    ResourcesConfig,
    ScalingConfig,
    ServiceNetwork,
    ServiceVolume,
)
from models.helm import WorkloadType
from models.k8s import RestartPolicy

from tests.fixtures.compose_cases import (
    ALL_NEW_FIELDS,
    BASIC_SERVICE,
    COMMAND_LIST_ONLY,
    COMMAND_ONLY,
    COMPLEX_MULTI_SERVICE,
    ENTRYPOINT_AND_COMMAND,
    ENTRYPOINT_ONLY,
    ENTRYPOINT_STRING,
    SINGLE_SERVICE_CASES,
    STOP_GRACE_PERIOD_COMBINED,
    WORKING_DIR,
)


class TestHelmValuesGeneratorBasic:
    """Basic tests for HelmValuesGenerator."""

    def test_generate_values_simple_compose(
        self,
        helm_generator: HelmValuesGenerator,
        simple_compose_file: ComposeFile,
    ):
        """Test generating Helm values from simple compose file."""
        values, warnings = helm_generator.generate_values(simple_compose_file)

        assert len(values.services) == 1
        assert values.services[0].name == "web"
        assert values.services[0].image.repository == "nginx"
        assert values.services[0].image.tag == "latest"

    def test_generate_values_includes_global_values(
        self,
        helm_generator: HelmValuesGenerator,
        simple_compose_file: ComposeFile,
    ):
        """Test global values include deployment/workspace IDs."""
        values, _ = helm_generator.generate_values(simple_compose_file)

        assert values.global_values is not None
        assert values.global_values.deploymentId is not None
        assert values.global_values.workspaceId is not None
        assert values.global_values.managedBy == "lazycloud"

    def test_generate_values_complex_compose(
        self,
        helm_generator: HelmValuesGenerator,
        complex_compose_file: ComposeFile,
    ):
        """Test generating Helm values from complex compose file."""
        values, _ = helm_generator.generate_values(complex_compose_file)

        assert len(values.services) == 3
        service_names = [s.name for s in values.services]
        assert "api" in service_names
        assert "postgres" in service_names
        assert "redis" in service_names


class TestServiceValuesGeneration:
    """Tests for service values generation."""

    def test_service_values_include_labels(
        self,
        helm_generator: HelmValuesGenerator,
        simple_compose_file: ComposeFile,
    ):
        """Test service values include proper labels."""
        values, _ = helm_generator.generate_values(simple_compose_file)

        service = values.services[0]
        assert "lazycloud.dev/workspace-id" in service.labels
        assert "lazycloud.dev/service" in service.labels
        assert service.labels["lazycloud.dev/managed-by"] == "lazycloud"

    def test_service_values_include_ports(
        self,
        helm_generator: HelmValuesGenerator,
        simple_compose_file: ComposeFile,
    ):
        """Test service values include port configuration."""
        values, _ = helm_generator.generate_values(simple_compose_file)

        service = values.services[0]
        assert service.ports is not None
        assert len(service.ports) == 1
        assert service.ports[0].port == 80

    def test_service_values_include_resources(
        self,
        helm_generator: HelmValuesGenerator,
    ):
        """Test service values include resource limits."""
        compose = ComposeFile(
            services=[
                ComposeService(
                    name="web",
                    image="nginx:latest",
                    deploy=DeployConfig(
                        resources=ResourcesConfig(
                            limits=ResourceConfig(cpus="1", memory="1Gi"),
                            reservations=ResourceConfig(cpus="0.5", memory="512Mi"),
                        )
                    ),
                )
            ]
        )

        values, _ = helm_generator.generate_values(compose)

        service = values.services[0]
        assert service.resources is not None
        assert service.resources.limits.cpu == "1"
        assert service.resources.limits.memory == "1Gi"

    def test_service_values_include_replicas(
        self,
        helm_generator: HelmValuesGenerator,
    ):
        """Test service values include replica count."""
        compose = ComposeFile(
            services=[
                ComposeService(
                    name="web",
                    image="nginx:latest",
                    deploy=DeployConfig(replicas=3),
                )
            ]
        )

        values, _ = helm_generator.generate_values(compose)

        assert values.services[0].replicas == 3


class TestWorkloadTypeGeneration:
    """Tests for workload type determination."""

    def test_deployment_workload_type_default(
        self,
        helm_generator: HelmValuesGenerator,
    ):
        """Test default workload type is Deployment."""
        compose = ComposeFile(
            services=[ComposeService(name="web", image="nginx:latest")]
        )

        values, _ = helm_generator.generate_values(compose)

        assert values.services[0].workloadType == WorkloadType.DEPLOYMENT

    def test_job_workload_type_for_restart_no(
        self,
        helm_generator: HelmValuesGenerator,
    ):
        """Test workload type is Job when restart policy is 'no'."""
        compose = ComposeFile(
            services=[
                ComposeService(
                    name="migration",
                    image="myapp:latest",
                    deploy=DeployConfig(restart_policy=RestartPolicy.NEVER),
                )
            ]
        )

        values, _ = helm_generator.generate_values(compose)

        assert values.services[0].workloadType == WorkloadType.JOB


class TestVolumeValuesGeneration:
    """Tests for volume values generation."""

    def test_generate_volume_values(
        self,
        helm_generator: HelmValuesGenerator,
    ):
        """Test generating volume values."""
        compose = ComposeFile(
            services=[
                ComposeService(
                    name="db",
                    image="postgres:15",
                    volumes=[
                        ServiceVolume(
                            type="volume", source="data", target="/var/lib/postgresql"
                        )
                    ],
                )
            ],
            volumes=[ComposeVolume(name="data")],
        )

        values, _ = helm_generator.generate_values(compose)

        assert len(values.volumes) == 1
        assert values.volumes[0].name == "data"
        assert values.volumes[0].enabled is True

    def test_volume_ebs_storage_class_single_service(
        self,
        helm_generator: HelmValuesGenerator,
    ):
        """Test EBS storage class for single-service volume."""
        compose = ComposeFile(
            services=[
                ComposeService(
                    name="db",
                    image="postgres:15",
                    volumes=[
                        ServiceVolume(type="volume", source="data", target="/data")
                    ],
                )
            ],
            volumes=[ComposeVolume(name="data")],
        )

        values, _ = helm_generator.generate_values(compose)

        # Single-service volume should use EBS (ReadWriteOnce)
        assert values.volumes[0].storageClass == STORAGE_CLASS_EBS
        assert "ReadWriteOnce" in values.volumes[0].accessModes

    def test_volume_efs_storage_class_shared_volume(
        self,
        helm_generator: HelmValuesGenerator,
        shared_volume_compose_file: ComposeFile,
    ):
        """Test EFS storage class for shared volume."""
        values, _ = helm_generator.generate_values(shared_volume_compose_file)

        # Shared volume should use EFS (ReadWriteMany)
        assert values.volumes[0].storageClass == STORAGE_CLASS_EFS
        assert "ReadWriteMany" in values.volumes[0].accessModes

    def test_volume_efs_from_label(
        self,
        helm_generator: HelmValuesGenerator,
    ):
        """Test EFS storage class from lazycloud.volume.shared label."""
        compose = ComposeFile(
            services=[
                ComposeService(
                    name="app",
                    image="myapp:latest",
                    volumes=[
                        ServiceVolume(type="volume", source="data", target="/data")
                    ],
                )
            ],
            volumes=[
                ComposeVolume(name="data", labels={"lazycloud.volume.shared": "true"})
            ],
        )

        values, _ = helm_generator.generate_values(compose)

        assert values.volumes[0].storageClass == STORAGE_CLASS_EFS

    def test_volume_size_from_label(
        self,
        helm_generator: HelmValuesGenerator,
    ):
        """Test volume size from lazycloud.volume.size label."""
        compose = ComposeFile(
            services=[
                ComposeService(
                    name="app",
                    image="myapp:latest",
                    volumes=[
                        ServiceVolume(type="volume", source="data", target="/data")
                    ],
                )
            ],
            volumes=[
                ComposeVolume(name="data", labels={"lazycloud.volume.size": "50Gi"})
            ],
        )

        values, _ = helm_generator.generate_values(compose)

        assert values.volumes[0].size == "50Gi"


class TestNetworkValuesGeneration:
    """Tests for network values generation."""

    def test_generate_network_values(
        self,
        helm_generator: HelmValuesGenerator,
    ):
        """Test generating network values."""
        compose = ComposeFile(
            services=[
                ComposeService(
                    name="api",
                    image="myapp:latest",
                    networks=[ServiceNetwork(name="backend")],
                )
            ],
            networks=[ComposeNetwork(name="backend")],
        )

        values, _ = helm_generator.generate_values(compose)

        assert len(values.networks) == 1
        assert values.networks[0].name == "backend"
        assert values.networks[0].enabled is True


class TestSecretsGeneration:
    """Tests for secrets generation in Helm values."""

    def test_generate_secrets_values(
        self,
        helm_generator_with_secrets: HelmValuesGenerator,
        simple_compose_file: ComposeFile,
    ):
        """Test generating secrets values."""
        values, _ = helm_generator_with_secrets.generate_values(simple_compose_file)

        assert len(values.secrets) == 1
        assert values.secrets[0].enabled is True
        assert "TEST_SECRET" in values.secrets[0].data


class TestScalingGeneration:
    """Tests for HPA/scaling values generation."""

    def test_generate_hpa_values(
        self,
        helm_generator: HelmValuesGenerator,
    ):
        """Test generating HPA values from scaling config."""
        compose = ComposeFile(
            services=[
                ComposeService(
                    name="api",
                    image="myapp:latest",
                    ports=[ComposePort(published=8080, target=8080)],
                    scaling=ScalingConfig(
                        enabled=True,
                        min=2,
                        max=10,
                        cpu="0.7",
                        memory="0.8",
                    ),
                )
            ]
        )

        values, _ = helm_generator.generate_values(compose)

        service = values.services[0]
        assert service.hpa is not None
        assert service.hpa.enabled is True
        assert service.hpa.minReplicas == 2
        assert service.hpa.maxReplicas == 10


class TestIngressGeneration:
    """Tests for ingress values generation."""

    def test_generate_ingress_for_service_with_ports(
        self,
        helm_generator: HelmValuesGenerator,
    ):
        """Test ingress is generated for services with ports."""
        compose = ComposeFile(
            services=[
                ComposeService(
                    name="api",
                    image="myapp:latest",
                    ports=[ComposePort(published=8080, target=8080)],
                )
            ]
        )

        values, _ = helm_generator.generate_values(compose)

        service = values.services[0]
        assert service.ingress is not None
        assert service.ingress.enabled is True


class TestGracePeriodGeneration:
    """Tests for graceful shutdown period generation."""

    def test_generate_grace_period(
        self,
        helm_generator: HelmValuesGenerator,
    ):
        """Test generating terminationGracePeriodSeconds."""
        compose = ComposeFile(
            services=[
                ComposeService(
                    name="api",
                    image="myapp:latest",
                    stop_grace_period=60,
                )
            ]
        )

        values, _ = helm_generator.generate_values(compose)

        assert values.services[0].terminationGracePeriodSeconds == 60


class TestValidationErrors:
    """Tests for validation error handling."""

    def test_validation_errors_raise_exception(
        self,
        helm_generator: HelmValuesGenerator,
    ):
        """Test validation errors raise ValueError."""
        # Compose with invalid service name
        compose = ComposeFile(
            services=[ComposeService(name="INVALID_NAME", image="nginx:latest")]
        )

        with pytest.raises(ValueError) as exc_info:
            helm_generator.generate_values(compose)

        assert "validation failed" in str(exc_info.value).lower()

    def test_missing_image_raises_exception(
        self,
        helm_generator: HelmValuesGenerator,
    ):
        """Test missing image raises ValueError."""
        compose = ComposeFile(services=[ComposeService(name="web", image="")])

        with pytest.raises(ValueError):
            helm_generator.generate_values(compose)


class TestBuildImageHandling:
    """Tests for build image handling."""

    def test_build_image_uses_depot_registry(
        self,
        helm_generator: HelmValuesGenerator,
    ):
        """Test build images use Depot registry URL."""
        compose = ComposeFile(
            services=[
                ComposeService(
                    name="app",
                    image="myapp:latest",
                    build=".",
                )
            ]
        )

        values, _ = helm_generator.generate_values(compose)

        # Should use Depot registry URL for build images
        # Format: registry.depot.dev/<project_id>/<image>
        assert "registry.depot.dev" in values.services[0].image.repository
        assert "test-depot-project" in values.services[0].image.repository
        assert values.services[0].image.pullPolicy == "Always"
        # Should have imagePullSecrets for Depot registry
        assert values.services[0].imagePullSecrets == [{"name": "depot-registry"}]


class TestCommandGeneration:
    """Tests for command generation."""

    def test_generate_command_from_list(
        self,
        helm_generator: HelmValuesGenerator,
    ):
        """Test generating command from list."""
        compose = ComposeFile(
            services=[
                ComposeService(
                    name="web",
                    image="nginx:latest",
                    command=["nginx", "-g", "daemon off;"],
                )
            ]
        )

        values, _ = helm_generator.generate_values(compose)

        # Command list should be split
        assert "nginx" in values.services[0].command

    def test_generate_command_from_string(
        self,
        helm_generator: HelmValuesGenerator,
    ):
        """Test generating command from string."""
        compose = ComposeFile(
            services=[
                ComposeService(
                    name="web",
                    image="nginx:latest",
                    command="python manage.py runserver",
                )
            ]
        )

        values, _ = helm_generator.generate_values(compose)

        assert "python" in values.services[0].command
        assert "manage.py" in values.services[0].command


class TestEndToEndPipeline:
    """Integration tests: YAML compose data → Parse → Helm values."""

    def test_basic_service_pipeline(self, helm_generator: HelmValuesGenerator):
        """Test basic service through full pipeline."""
        case = BASIC_SERVICE
        compose_file = ComposeParser.parse_dict(case["data"])

        values, _ = helm_generator.generate_values(compose_file)

        expected = case["expected_helm"]
        service = values.services[0]

        assert service.name == expected["name"]
        assert service.command == expected["command"]
        assert service.args == expected["args"]
        assert service.workingDir == expected["workingDir"]
        assert (
            service.terminationGracePeriodSeconds
            == expected["terminationGracePeriodSeconds"]
        )

    def test_entrypoint_only_pipeline(self, helm_generator: HelmValuesGenerator):
        """Test entrypoint only maps to K8s command."""
        case = ENTRYPOINT_ONLY
        compose_file = ComposeParser.parse_dict(case["data"])

        values, _ = helm_generator.generate_values(compose_file)

        expected = case["expected_helm"]
        service = values.services[0]

        assert service.command == expected["command"]
        assert service.args == expected["args"]

    def test_entrypoint_string_pipeline(self, helm_generator: HelmValuesGenerator):
        """Test entrypoint as string maps to K8s command list."""
        case = ENTRYPOINT_STRING
        compose_file = ComposeParser.parse_dict(case["data"])

        values, _ = helm_generator.generate_values(compose_file)

        expected = case["expected_helm"]
        service = values.services[0]

        assert service.command == expected["command"]
        assert service.args == expected["args"]

    def test_command_only_pipeline(self, helm_generator: HelmValuesGenerator):
        """Test command only (no entrypoint) maps to K8s command."""
        case = COMMAND_ONLY
        compose_file = ComposeParser.parse_dict(case["data"])

        values, _ = helm_generator.generate_values(compose_file)

        expected = case["expected_helm"]
        service = values.services[0]

        # When only command exists, it becomes K8s command
        assert service.command == expected["command"]
        assert service.args == expected["args"]

    def test_command_list_only_pipeline(self, helm_generator: HelmValuesGenerator):
        """Test command as list (no entrypoint) maps to K8s command."""
        case = COMMAND_LIST_ONLY
        compose_file = ComposeParser.parse_dict(case["data"])

        values, _ = helm_generator.generate_values(compose_file)

        expected = case["expected_helm"]
        service = values.services[0]

        assert service.command == expected["command"]
        assert service.args == expected["args"]

    def test_entrypoint_and_command_pipeline(self, helm_generator: HelmValuesGenerator):
        """Test entrypoint + command maps to K8s command + args."""
        case = ENTRYPOINT_AND_COMMAND
        compose_file = ComposeParser.parse_dict(case["data"])

        values, _ = helm_generator.generate_values(compose_file)

        expected = case["expected_helm"]
        service = values.services[0]

        # Entrypoint → K8s command
        assert service.command == expected["command"]
        # Command → K8s args
        assert service.args == expected["args"]

    def test_working_dir_pipeline(self, helm_generator: HelmValuesGenerator):
        """Test working_dir flows through pipeline."""
        case = WORKING_DIR
        compose_file = ComposeParser.parse_dict(case["data"])

        values, _ = helm_generator.generate_values(compose_file)

        expected = case["expected_helm"]
        service = values.services[0]

        assert service.workingDir == expected["workingDir"]

    def test_stop_grace_period_pipeline(self, helm_generator: HelmValuesGenerator):
        """Test stop_grace_period duration parsing flows through pipeline."""
        case = STOP_GRACE_PERIOD_COMBINED
        compose_file = ComposeParser.parse_dict(case["data"])

        values, _ = helm_generator.generate_values(compose_file)

        expected = case["expected_helm"]
        service = values.services[0]

        # "1m30s" should be parsed to 90 seconds
        assert (
            service.terminationGracePeriodSeconds
            == expected["terminationGracePeriodSeconds"]
        )

    def test_all_new_fields_pipeline(self, helm_generator: HelmValuesGenerator):
        """Test all new fields together through full pipeline."""
        case = ALL_NEW_FIELDS
        compose_file = ComposeParser.parse_dict(case["data"])

        values, _ = helm_generator.generate_values(compose_file)

        expected = case["expected_helm"]
        service = values.services[0]

        assert service.name == expected["name"]
        assert service.command == expected["command"]
        assert service.args == expected["args"]
        assert service.workingDir == expected["workingDir"]
        assert (
            service.terminationGracePeriodSeconds
            == expected["terminationGracePeriodSeconds"]
        )

    def test_complex_multi_service_pipeline(self, helm_generator: HelmValuesGenerator):
        """Test complex multi-service through full pipeline."""
        case = COMPLEX_MULTI_SERVICE
        compose_file = ComposeParser.parse_dict(case["data"])

        values, _ = helm_generator.generate_values(compose_file)

        expected_services = case["expected_helm"]["services"]

        # Should have 3 services
        assert len(values.services) == 3

        # Find and verify each service
        for expected in expected_services:
            service = next(
                (s for s in values.services if s.name == expected["name"]), None
            )
            assert service is not None, f"Service {expected['name']} not found"

            assert service.command == expected["command"]
            assert service.args == expected["args"]
            assert service.workingDir == expected["workingDir"]
            assert (
                service.terminationGracePeriodSeconds
                == expected["terminationGracePeriodSeconds"]
            )

    @pytest.mark.parametrize(
        "case",
        SINGLE_SERVICE_CASES,
        ids=[c["name"] for c in SINGLE_SERVICE_CASES],
    )
    def test_parametrized_single_service_pipeline(
        self, helm_generator: HelmValuesGenerator, case: dict[str, Any]
    ):
        """Parametrized test for all single-service cases through full pipeline."""
        compose_file = ComposeParser.parse_dict(case["data"])

        values, _ = helm_generator.generate_values(compose_file)

        expected = case["expected_helm"]
        service = values.services[0]

        assert service.name == expected["name"]

        if "command" in expected:
            assert service.command == expected["command"]
        if "args" in expected:
            assert service.args == expected["args"]
        if "workingDir" in expected:
            assert service.workingDir == expected["workingDir"]
        if "terminationGracePeriodSeconds" in expected:
            assert (
                service.terminationGracePeriodSeconds
                == expected["terminationGracePeriodSeconds"]
            )
