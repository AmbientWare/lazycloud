from typing import Any

from loguru import logger

from shared.models.compose import (
    ComposeFile,
    ComposeNetwork,
    ComposePort,
    ComposeService,
    ComposeVolume,
    DeployConfig,
    HealthCheck,
    ResourceConfig,
    ResourcesConfig,
    ScalingConfig,
    ServiceNetwork,
    ServiceVolume,
)


def _get_int(scaling_labels, key, default=None):
    return int(scaling_labels[key]) if key in scaling_labels else default


class ComposeParser:
    """Parser for Docker Compose v3 files. NOTE: right now we only support > v3 files."""

    @staticmethod
    def parse_dict(
        data: dict[str, Any],
    ) -> ComposeFile:
        version = data.get("version")
        # None can only works in latest version of compose file
        if version is not None and not version.startswith("3"):
            raise ValueError(
                f"Unsupported compose version: {version}. Only version 3.x is supported, or omit version for modern compose files."
            )

        # Parse volumes
        volumes = [
            ComposeParser._parse_volume_definition(volume_config)
            for volume_config in data.get("volumes", [])
            if volume_config is not None
        ]

        # Parse networks
        networks = [
            ComposeParser._parse_network(network_config)
            for network_config in data.get("networks", [])
            if network_config is not None
        ]

        # Parse services
        services = [
            ComposeParser._parse_service(
                service_name, service_config, networks, volumes
            )
            for service_name, service_config in data.get("services", {}).items()
            if service_config is not None
        ]

        # Create ComposeFile with user context
        compose_file = ComposeFile(
            version=version,
            services=services,
            networks=networks,
            volumes=volumes,
        )

        return compose_file

    @staticmethod
    def _parse_service(
        service_name: str,
        config: dict[str, Any],
        networks: list[ComposeNetwork],
        volumes: list[ComposeVolume],
    ) -> ComposeService:
        """Parse a single service configuration."""
        # Parse each component with dedicated methods
        ports = ComposeParser._parse_service_ports(config.get("ports", []))
        service_volumes = ComposeParser._parse_service_volumes(
            config.get("volumes", []), {v.name for v in volumes}
        )
        service_networks = ComposeParser._parse_service_networks(
            config.get("networks"), {n.name for n in networks}
        )

        deploy, scaling = ComposeParser._parse_deploy_and_scaling(
            config.get("deploy", {}), service_name
        )

        healthcheck = ComposeParser._parse_healthcheck(config.get("healthcheck", {}))

        # Parse command - convert list to string if needed
        command = config.get("command")
        if isinstance(command, list):
            command = " ".join(command)

        return ComposeService(
            name=service_name,
            image=config.get("image"),
            command=command,
            ports=ports or None,
            volumes=service_volumes or None,
            networks=service_networks or None,
            deploy=deploy,
            healthcheck=healthcheck,
            scaling=scaling or ScalingConfig(),
        )

    @staticmethod
    def _parse_service_ports(ports_config: list) -> list[ComposePort]:
        """Parse service ports configuration."""
        ports = []
        for port_config in ports_config:
            if isinstance(port_config, str):
                port = ComposeParser._parse_port_string(port_config)
                if port:
                    ports.append(port)
            elif isinstance(port_config, dict):
                ports.append(ComposePort(**port_config))
            elif isinstance(port_config, int):
                ports.append(ComposePort(target=port_config))
        return ports

    @staticmethod
    def _parse_service_volumes(
        volumes_config: list, defined_volumes: set[str]
    ) -> list[ServiceVolume]:
        """Parse service volumes, only including defined named volumes."""
        service_volumes = []
        for volume_config in volumes_config:
            volume = None
            if isinstance(volume_config, str):
                volume = ComposeParser._parse_volume_string(volume_config)
            elif isinstance(volume_config, dict):
                volume = ServiceVolume(**volume_config)

            if (
                volume
                and volume.type == "volume"
                and volume.source
                and volume.source in defined_volumes
            ):
                # only support named volumes
                service_volumes.append(volume)
        return service_volumes

    @staticmethod
    def _parse_service_networks(
        networks_config: dict | list | None, defined_networks: set[str]
    ) -> list[ServiceNetwork]:
        """Parse service networks configuration."""
        if not networks_config:
            return []

        service_networks = []
        if isinstance(networks_config, list):
            # Simple list format
            service_networks = [
                ServiceNetwork(name=net)
                for net in networks_config
                if net in defined_networks
            ]
        elif isinstance(networks_config, dict):
            # Dict format with possible aliases
            for net_name, net_config in networks_config.items():
                if net_name in defined_networks:
                    aliases = (
                        net_config.get("aliases")
                        if isinstance(net_config, dict)
                        else None
                    )
                    service_networks.append(
                        ServiceNetwork(name=net_name, aliases=aliases)
                    )
        return service_networks

    @staticmethod
    def _parse_deploy_and_scaling(
        deploy_config: dict, service_name: str
    ) -> tuple[DeployConfig, ScalingConfig | None]:
        """Parse deploy configuration and extract scaling from labels."""
        if not deploy_config:
            return DeployConfig(), None

        # Build deploy configuration
        deploy_kwargs = {}

        # Add replicas if specified
        if "replicas" in deploy_config:
            deploy_kwargs["replicas"] = deploy_config["replicas"]

        # Parse resources
        resources_config = deploy_config.get("resources", {})
        if resources_config:
            resources = ComposeParser._parse_resources(resources_config, service_name)
            if resources:
                deploy_kwargs["resources"] = resources

        deploy = DeployConfig(**deploy_kwargs)

        # Parse scaling from labels
        scaling = None
        labels = deploy_config.get("labels", {})
        if isinstance(labels, dict):
            scaling = ComposeParser._parse_scaling_labels(labels)

        return deploy, scaling

    @staticmethod
    def _parse_resources(
        resources_config: dict, service_name: str
    ) -> ResourcesConfig | None:
        """Parse resource limits and reservations."""
        cleaned_resources = {}

        for resource_type in ["limits", "reservations"]:
            if resource_type in resources_config:
                try:
                    cleaned_resources[resource_type] = ResourceConfig(
                        **resources_config[resource_type]
                    )
                except Exception as e:
                    logger.warning(
                        f"Invalid {resource_type} config for {service_name}: {e}"
                    )

        return ResourcesConfig(**cleaned_resources) if cleaned_resources else None

    @staticmethod
    def _parse_scaling_labels(labels: dict) -> ScalingConfig | None:
        """Parse LazyCloud scaling configuration from labels."""
        scaling_labels = {
            k: v for k, v in labels.items() if k.startswith("lazycloud.scaling.")
        }

        if not scaling_labels:
            return None

        return ScalingConfig(
            enabled=scaling_labels.get("lazycloud.scaling.enabled", "false").lower()
            == "true",
            min=_get_int(scaling_labels, "lazycloud.scaling.min"),
            max=_get_int(scaling_labels, "lazycloud.scaling.max"),
            cpu=scaling_labels.get("lazycloud.scaling.cpu"),
            memory=scaling_labels.get("lazycloud.scaling.memory"),
        )

    @staticmethod
    def _parse_healthcheck(healthcheck_config: dict) -> HealthCheck:
        """Parse healthcheck configuration."""
        if not healthcheck_config:
            return HealthCheck()

        # Only include fields that are actually present in the config
        healthcheck_fields = [
            "test",
            "interval",
            "timeout",
            "retries",
            "start_period",
            "disable",
        ]
        healthcheck_data = {
            field: healthcheck_config[field]
            for field in healthcheck_fields
            if field in healthcheck_config
        }

        return HealthCheck(**healthcheck_data)

    @staticmethod
    def _parse_port_string(port_str: str) -> ComposePort | None:
        """Parse a port string like '8080:80', '80', '8080:80/udp', or '80/tcp' into a ComposePort."""
        try:
            # First check if there's a protocol suffix
            protocol = "tcp"  # default protocol
            port_part = port_str

            if "/" in port_str:
                port_part, protocol = port_str.rsplit("/", 1)
                # Validate protocol
                if protocol.lower() not in ["tcp", "udp", "sctp"]:
                    return None

                protocol = protocol.lower()

            # Now parse the port part
            parts = port_part.split(":")
            if len(parts) == 1:
                # Just target port (e.g., "80" or "80/udp")
                return ComposePort(
                    published=int(parts[0]), target=int(parts[0]), protocol=protocol
                )

            elif len(parts) == 2:
                # host:target format (e.g., "8080:80" or "8080:80/udp")
                # We only care about the target port for Kubernetes
                return ComposePort(
                    published=int(parts[0]), target=int(parts[1]), protocol=protocol
                )

            else:
                # Invalid format
                return None

        except ValueError:
            return None

    @staticmethod
    def _parse_volume_string(volume_str: str) -> ServiceVolume | None:
        """Parse a volume string like 'myvolume:/data' or '/host/path:/container/path'."""
        parts = volume_str.split(":")
        if len(parts) < 2:
            # Invalid format
            return None

        source = parts[0]
        target = parts[1]

        # Check if it's a bind mount (starts with / or .)
        if source.startswith(("/", ".", "~")):
            # Bind mount - type "bind"
            return ServiceVolume(
                type="bind",
                source=source,
                target=target,
                read_only=len(parts) > 2 and parts[2] == "ro",
            )
        else:
            # Named volume
            return ServiceVolume(
                type="volume",
                source=source,
                target=target,
                read_only=len(parts) > 2 and parts[2] == "ro",
            )

    @staticmethod
    def _parse_network(config: dict[str, Any] | str) -> ComposeNetwork:
        """Parse a network configuration."""
        if isinstance(config, str):
            return ComposeNetwork(
                name=config,
            )
        else:
            return ComposeNetwork(
                name=config.get("name"),
            )

    @staticmethod
    def _parse_volume_definition(config: dict[str, Any] | str) -> ComposeVolume:
        """Parse a volume definition."""
        if isinstance(config, str):
            return ComposeVolume(
                name=config,
            )
        else:
            return ComposeVolume(
                name=config.get("name"),
            )

    def validate_for_k8s(self, compose: ComposeFile) -> list[str]:
        """Validate compose file for Kubernetes translation and return warnings."""
        warnings = []

        # Check for unsupported features
        for service in compose.services:
            # Check for bind mounts (we only support named volumes)
            if service.volumes:
                for volume in service.volumes:
                    if isinstance(volume, str):
                        parts = volume.split(":")
                        if len(parts) >= 2 and parts[0].startswith(("/", ".", "~")):
                            # This is a bind mount
                            warnings.append(
                                f"Service '{service.name}': Bind mount '{volume}' will be ignored (only named volumes supported)"
                            )
                    elif isinstance(volume, ServiceVolume) and volume.type == "bind":
                        warnings.append(
                            f"Service '{service.name}': Bind mount will be ignored (only named volumes supported)"
                        )

        return warnings
