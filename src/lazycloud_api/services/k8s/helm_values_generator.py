"""
Helm values generator from Docker Compose files.
Converts compose services to Helm chart values instead of raw Kubernetes manifests.
"""

from lazycloud_api.config import app_config
from lazycloud_api.database import db
from lazycloud_api.database.compose import ComposeDeploymentPydantic
from lazycloud_api.services.compose.validator import ComposeValidator
from lazycloud_api.services.k8s.generators.configuration import (
    generate_healthcheck_values,
    generate_service_volumes_values,
)
from lazycloud_api.services.k8s.generators.converters import (
    convert_memory_value,
)
from lazycloud_api.services.k8s.generators.monitoring import (
    generate_hpa_values,
)
from lazycloud_api.services.k8s.generators.networking import (
    ParsedPort,
    generate_ingress_values,
    generate_petname,
    generate_ports_values,
    parse_port_string,
)
from lazycloud_api.services.k8s.generators.workloads import (
    generate_pod_security_context_values,
    generate_resources_values,
    generate_security_context_values,
    parse_image,
    should_be_statefulset,
)
from shared.models.compose import (
    ComposeFile,
    ComposeNetwork,
    ComposeService,
    ComposeVolume,
    DeployConfig,
)
from shared.models.helm import (
    GlobalValues,
    HelmValues,
    NetworkValues,
    RestartPolicy,
    SecretValues,
    ServiceValues,
    VolumeValues,
    WorkloadType,
)


class HelmValuesGenerator:
    """Generates Helm chart values from Docker Compose configurations."""

    def __init__(self, deployment: ComposeDeploymentPydantic):
        self.deployment = deployment
        self._secrets = self._load_secrets()

    def _load_secrets(self) -> dict[str, str] | None:
        """Load secrets from database if deployment_id is available."""
        if self.deployment.id:
            secret = db.secrets.get_secret(self.deployment.id)
            if secret:
                return secret.secrets

        return None

    def generate_values(self, compose: ComposeFile) -> tuple[HelmValues, list[str]]:
        """Generate Helm values from a compose file."""
        errors, warnings = ComposeValidator().validate(compose)
        if errors:
            error_messages = [str(error) for error in errors]
            raise ValueError(
                f"Compose file validation failed with {len(errors)} error(s):\n"
                + "\n".join(f"  - {msg}" for msg in error_messages)
            )

        # Generate global values with user context
        global_values = GlobalValues(
            deploymentId=self.deployment.id,
            userId=self.deployment.user_id,
            managedBy="lazycloud",
            createdBy="lazycloud-api",
            runtimeClassName="gvisor",
            labels={
                "lazycloud.io/deployment-id": self.deployment.id,
                "lazycloud.io/user-id": self.deployment.user_id,
                "lazycloud.io/managed-by": "lazycloud",
            },
            annotations={
                "lazycloud.io/deployment-id": self.deployment.id,
                "lazycloud.io/user-id": self.deployment.user_id,
                "lazycloud.io/created-by": "lazycloud-api",
            },
        )

        values = HelmValues(
            global_values=global_values,  # type: ignore
            services=[],
            networks=[],
            volumes=[],
            secrets=[],
        )

        # Collect all environment variables from all services
        all_env_vars = {}
        for service in compose.services:
            service_values, env_secret_data = self._generate_service_values(
                service, compose
            )
            values.services.append(service_values)

            # Collect env vars for deployment-wide secret
            if env_secret_data:
                all_env_vars.update(env_secret_data)

        # Create a single deployment-wide secret for all env vars
        if all_env_vars and self.deployment.id:
            # Use deployment ID in the secret name to ensure uniqueness
            secret_name = f"env-{self.deployment.id[:8]}"
            values.secrets.append(
                SecretValues(
                    name=secret_name,
                    enabled=True,
                    type="Opaque",
                    data=all_env_vars,
                )
            )

        # Generate networks values
        if compose.networks:
            for network in compose.networks:
                values.networks.append(self._generate_network_values(network))

        # Generate volumes values
        if compose.volumes:
            for volume in compose.volumes:
                values.volumes.append(self._generate_volume_values(volume))

        warning_messages = [str(warning) for warning in warnings]
        return values, warning_messages

    def _generate_service_values(
        self, service: ComposeService, compose: ComposeFile
    ) -> tuple[ServiceValues, dict[str, str] | None]:
        """Generate Helm values for a single service."""

        if not service.image:
            raise ValueError(f"Service {service.name} has no image")

        # Parse image
        image_info = parse_image(service.image)

        # Apply registry configuration to image
        registry = app_config.registry
        image_info.repository = registry.format_image(image_info.repository)
        image_info.pullPolicy = registry.pull_policy

        service_values = ServiceValues(
            name=service.name,
            enabled=True,
            image=image_info,
            resourceName=service.name,
            labels={
                "lazycloud.io/user-id": self.deployment.user_id,
                "lazycloud.io/service": service.name,
                "lazycloud.io/managed-by": "lazycloud",
            },
            annotations={
                "lazycloud.io/user-id": self.deployment.user_id,
                "lazycloud.io/created-by": "lazycloud-api",
            },
        )

        # Add deployment ID to labels if available
        if self.deployment.id:
            service_values.labels["lazycloud.io/deployment-id"] = self.deployment.id
            service_values.annotations["lazycloud.io/deployment-id"] = (
                self.deployment.id
            )

        # Set workload type
        # Since labels are now in deploy config for scaling, use empty dict for workload detection
        is_statefulset = should_be_statefulset(service.image, service.name)
        if is_statefulset:
            service_values.workloadType = WorkloadType.STATEFULSET.value
            service_values.serviceName = service.name
        else:
            service_values.workloadType = WorkloadType.DEPLOYMENT.value

        # Add command if specified
        if service.command:
            if isinstance(service.command, list):
                service_values.command = service.command
            else:
                service_values.command = service.command.split()

        # Add ports
        if service.ports:
            port_configs = generate_ports_values(service.ports)
            service_values.ports = port_configs

        # Add volumes
        if service.volumes:
            volume_mounts = generate_service_volumes_values(service.volumes, compose)
            service_values.volumes = volume_mounts

        # Add restart policy
        # Restart policy is derived from deploy config
        restart_policy = self._map_restart_policy(service.deploy)

        # Kubernetes Deployments and StatefulSets MUST have restartPolicy: Always
        # If the service needs "Never" (run once), it should be a Job, but Jobs aren't
        # supported yet in the Helm templates. For now, we'll use "Always" for compatibility.
        # TODO: Add Job support for one-shot containers
        if restart_policy in [RestartPolicy.NEVER, RestartPolicy.ON_FAILURE]:
            # Log a warning or add a comment that this service might be better as a Job
            service_values.restartPolicy = RestartPolicy.ALWAYS.value
            service_values.annotations["lazycloud.io/intended-restart-policy"] = (
                service.deploy.restart_policy.value
            )
            service_values.annotations["lazycloud.io/note"] = (
                "Should be Job when supported"
            )
        else:
            service_values.restartPolicy = RestartPolicy.ALWAYS.value

        # Add resources
        if service.deploy and service.deploy.resources:
            resources = generate_resources_values(service.deploy.resources)
            if resources:
                service_values.resources = resources

        # Add health checks
        if service.healthcheck:
            healthcheck_values = generate_healthcheck_values(
                service.healthcheck, service.name
            )
            service_values.healthcheck = healthcheck_values

        # Add ingress configuration
        ingress_config = generate_ingress_values(service)
        if ingress_config:
            service_values.ingress = ingress_config

        # Add HPA configuration
        hpa_config = generate_hpa_values(service)
        if hpa_config:
            service_values.hpa = hpa_config

        # Add replicas only if HPA is not enabled
        # When HPA is enabled, it manages the replica count
        if service.scaling and service.scaling.enabled:
            if not hpa_config or not hpa_config.enabled:
                service_values.replicas = service.scaling.min

        # Add metrics configuration
        # TODO: Add metrics configuration
        # metrics_config = generate_metrics_values(service.ports)
        # if metrics_config:
        #     service_values.metrics = metrics_config

        # Add networks for namespace determination
        if service.networks:
            service_values.networks = service.networks

        # Generate security contexts
        security_context = generate_security_context_values()
        service_values.securityContext = security_context

        # Generate pod security context
        has_volumes = bool(service.volumes)
        pod_security_context = generate_pod_security_context_values(has_volumes)
        service_values.podSecurityContext = pod_security_context

        return service_values, self._secrets if self._secrets else None

    def _map_restart_policy(self, deploy_config: DeployConfig | None) -> str:
        """Map Docker Compose restart policy to Kubernetes restart policy."""
        # Check deploy.restart_policy if present
        if deploy_config and deploy_config.restart_policy:
            # For now, we always use "Always" for Kubernetes deployments
            # This is because Deployments and StatefulSets require this policy
            return deploy_config.restart_policy

        # Default to Always
        return RestartPolicy.ALWAYS.value

    def _generate_network_values(self, network: ComposeNetwork) -> NetworkValues:
        """Generate Helm values for a network."""
        # NOTE: network parameter is kept for future extensibility
        # Currently all networks are treated the same way
        network_values = NetworkValues(
            name=network.name,
            enabled=True,
            external=False,  # We don't support external networks
        )
        return network_values

    def _generate_volume_values(self, volume: ComposeVolume) -> VolumeValues:
        """Generate Helm values for a volume."""
        # NOTE: volume parameter is kept for future extensibility (e.g., for size hints)
        # Currently all volumes get default values
        volume_values = VolumeValues(
            name=volume.name,
            enabled=True,
            size="1Gi",  # Note: in cloud this will be EFS, size is ignored
            accessModes=["ReadWriteOnce"],
            labels={
                "lazycloud.io/user-id": self.deployment.user_id,
                "lazycloud.io/managed-by": "lazycloud",
            },
            annotations={
                "lazycloud.io/user-id": self.deployment.user_id,
                "lazycloud.io/created-by": "lazycloud-api",
            },
        )
        return volume_values

    def validate_for_helm(self, compose: ComposeFile) -> list[str]:
        """Validate compose file for Helm chart deployment and return warnings."""
        warnings = []

        # Check for unsupported features
        defined_volumes = compose.volumes or {}
        for service in compose.services:
            # Check for bind mounts and undefined volumes
            if service.volumes:
                for volume in service.volumes:
                    if isinstance(volume, str):
                        if ":" in volume:
                            parts = volume.split(":")
                            if len(parts) >= 2:
                                volume_source = parts[0]
                                # Check for bind mounts (start with / or .)
                                if volume_source.startswith(("/", ".", "~")):
                                    warnings.append(
                                        f"Service '{service.name}': Bind mount '{volume}' not supported in Kubernetes. Use named volumes instead."
                                    )
                                # Check for undefined named volumes
                                elif volume_source not in defined_volumes:
                                    warnings.append(
                                        f"Service '{service.name}': Volume '{volume_source}' not defined in volumes section"
                                    )

        # We don't support external networks or volumes, but our simplified model
        # already filters these out during parsing

        return warnings

    # Expose utility methods for testing
    def _parse_port_string(self, port_str: str) -> ParsedPort:
        """Parse a port string - wrapper for testing."""
        return parse_port_string(port_str)

    def _convert_memory_value(self, memory_value: str | int) -> str:
        """Convert memory value - wrapper for testing."""
        return convert_memory_value(memory_value)

    def _generate_petname(self, service_name: str) -> str:
        """Generate petname - wrapper for testing."""
        return generate_petname(service_name)

    def _should_be_statefulset(
        self,
        image: str,
        service_name: str,
    ) -> bool:
        """Check if should be StatefulSet - wrapper for testing."""
        # Note: volumes parameter kept for backward compatibility with tests
        return should_be_statefulset(image, service_name)

    def _parse_image(self, image_string: str) -> dict[str, str]:
        """Parse image string - wrapper for testing."""
        return parse_image(image_string).model_dump()
