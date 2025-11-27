from lazycloud_api.database.compose import ComposeDeploymentPydantic
from lazycloud_api.database.secrets import SecretPydantic
from lazycloud_api.services import get_ecr_auth_service
from lazycloud_api.services.compose.diff_checker import get_shared_volumes
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
)
from shared.models.billing import STORAGE_CLASS_EBS, STORAGE_CLASS_EFS
from shared.models.compose import (
    ComposeFile,
    ComposeNetwork,
    ComposeService,
    ComposeVolume,
    DeployConfig,
    LazyCloudLabel,
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

    def __init__(
        self, deployment: ComposeDeploymentPydantic, secrets: list[SecretPydantic]
    ):
        self.deployment = deployment
        self._secrets = (
            {secret.key: secret.value for secret in secrets} if secrets else {}
        )

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
            deploymentId=str(self.deployment.id),
            workspaceId=str(self.deployment.workspace_id),
            managedBy="lazycloud",
            createdBy="lazycloud-api",
            runtimeClassName="gvisor",
            labels={
                "lazycloud.dev/deployment-id": str(self.deployment.id),
                "lazycloud.dev/workspace-id": str(self.deployment.workspace_id),
                "lazycloud.dev/managed-by": "lazycloud",
            },
            annotations={
                "lazycloud.dev/deployment-id": str(self.deployment.id),
                "lazycloud.dev/workspace-id": str(self.deployment.workspace_id),
                "lazycloud.dev/created-by": "lazycloud-api",
            },
        )

        values = HelmValues(
            global_values=global_values,
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
            shared_volumes = get_shared_volumes(compose)
            for volume in compose.volumes:
                is_shared = volume.name in shared_volumes
                values.volumes.append(self._generate_volume_values(volume, is_shared))

        warning_messages = [str(warning) for warning in warnings]
        return values, warning_messages

    def _generate_service_values(
        self, service: ComposeService, compose: ComposeFile
    ) -> tuple[ServiceValues, dict[str, str] | None]:
        """Generate Helm values for a single service."""
        ecr_auth_service = get_ecr_auth_service()

        if not service.image:
            raise ValueError(f"Service {service.name} has no image")

        image_info = parse_image(service.image)

        if service.build is not None:
            image_info.pullPolicy = ecr_auth_service.get_pull_policy()
            image_info.repository = ecr_auth_service.get_repository_url(
                workspace_id=self.deployment.workspace_id,
                deployment_name=self.deployment.name,
                image_name=image_info.repository,
            )

        service_values = ServiceValues(
            replicas=service.deploy.replicas,
            # resources will be set later after conversion
            restartPolicy=service.deploy.restart_policy,
            # healthcheck and hpa will be set later after conversion
            name=service.name,
            enabled=True,
            image=image_info,
            resourceName=service.name,
            labels={
                "lazycloud.dev/workspace-id": self.deployment.workspace_id,
                "lazycloud.dev/service": service.name,
                "lazycloud.dev/managed-by": "lazycloud",
            },
            annotations={
                "lazycloud.dev/workspace-id": self.deployment.workspace_id,
                "lazycloud.dev/created-by": "lazycloud-api",
            },
        )

        # Add deployment ID to labels if available
        if self.deployment.id:
            service_values.labels["lazycloud.dev/deployment-id"] = self.deployment.id
            service_values.annotations["lazycloud.dev/deployment-id"] = (
                self.deployment.id
            )

        # Add restart policy first to determine workload type
        restart_policy = self._map_restart_policy(service.deploy)

        # Set workload type based on restart policy
        # Services with restart: "no" (NEVER) should be Jobs (one-time execution)
        if restart_policy == RestartPolicy.NEVER:
            service_values.workloadType = WorkloadType.JOB
            service_values.restartPolicy = RestartPolicy.NEVER.value
        else:
            service_values.workloadType = WorkloadType.DEPLOYMENT
            service_values.restartPolicy = RestartPolicy.ALWAYS.value

        # Handle entrypoint and command mapping to K8s command/args
        # In K8s: command = entrypoint, args = command
        if service.entrypoint:
            # If entrypoint exists, it becomes K8s command
            if isinstance(service.entrypoint, list):
                service_values.command = service.entrypoint
            else:
                service_values.command = service.entrypoint.split()

            # And command becomes K8s args
            if service.command:
                if isinstance(service.command, list):
                    service_values.args = service.command
                else:
                    service_values.args = service.command.split()
                    
        elif service.command:
            # No entrypoint, command becomes K8s command (existing behavior)
            if isinstance(service.command, list):
                service_values.command = service.command
            else:
                service_values.command = service.command.split()

        # Add working directory if specified
        if service.working_dir:
            service_values.workingDir = service.working_dir

        # Add ports
        if service.ports:
            port_configs = generate_ports_values(service.ports)
            service_values.ports = port_configs

        # Add volumes
        if service.volumes:
            volume_mounts = generate_service_volumes_values(service.volumes, compose)
            service_values.volumes = volume_mounts

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

        # NOTE: ONLY ADD REPLICAS IF HPA IS NOT ENABLED !!! HPA MANAGES THE REPLICAS
        # When HPA is enabled, it manages the replica count
        if service.scaling and service.scaling.enabled:
            if not hpa_config or not hpa_config.enabled:
                service_values.replicas = service.scaling.min

        # Add metrics configuration
        # TODO: Add metrics configuration (not supported yet)
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

        # Add graceful shutdown period if specified
        if service.stop_grace_period is not None:
            service_values.terminationGracePeriodSeconds = service.stop_grace_period

        return service_values, self._secrets if self._secrets else None

    def _map_restart_policy(self, deploy_config: DeployConfig | None) -> str:
        """Map Docker Compose restart policy to Kubernetes restart policy."""
        # Check deploy.restart_policy if present
        if deploy_config and deploy_config.restart_policy:
            # For now, we always use "Always" for Kubernetes deployments
            # This is because Deployments require this policy
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

    def _generate_volume_values(
        self, volume: ComposeVolume, is_shared: bool = False
    ) -> VolumeValues:
        """Generate Helm values for a volume."""
        volume_labels = volume.labels or {}

        # Explicit shared label OR auto-detected shared (multi-service usage)
        use_shared = (
            volume_labels.get(LazyCloudLabel.VOLUME_SHARED) == "true" or is_shared
        )

        # Size from label or default 10Gi
        size = volume_labels.get(LazyCloudLabel.STORAGE_SIZE, "10Gi")

        # Select storage class and access mode based on shared status
        storage_class = STORAGE_CLASS_EFS if use_shared else STORAGE_CLASS_EBS
        access_mode = "ReadWriteMany" if use_shared else "ReadWriteOnce"

        # Merge user labels with system labels
        merged_labels = {
            "lazycloud.dev/workspace-id": self.deployment.workspace_id,
            "lazycloud.dev/managed-by": "lazycloud",
        }
        if volume_labels:
            merged_labels.update(volume_labels)

        volume_values = VolumeValues(
            name=volume.name,
            enabled=True,
            size=size,
            accessModes=[access_mode],
            storageClass=storage_class,
            labels=merged_labels,
            annotations={
                "lazycloud.dev/workspace-id": self.deployment.workspace_id,
                "lazycloud.dev/created-by": "lazycloud-api",
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

    def _parse_image(self, image_string: str) -> dict[str, str]:
        """Parse image string - wrapper for testing."""
        return parse_image(image_string).model_dump()
