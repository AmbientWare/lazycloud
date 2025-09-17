"""
Docker Compose v3 parser for LazyCloud.
Parses docker-compose.yml files and extracts services, networks, volumes, and secrets.
"""

from pathlib import Path
from typing import Any

import yaml
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


class ComposeParser:
    """Parser for Docker Compose v3 files."""

    @staticmethod
    def parse_file(file_path: str | Path, user_id: str | None = None) -> ComposeFile:
        """Parse a docker-compose.yml file."""
        file_path = Path(file_path)

        if not file_path.exists():
            raise FileNotFoundError(f"Compose file not found: {file_path}")

        with open(file_path, "r") as f:
            data = yaml.safe_load(f)

        return ComposeParser.parse_dict(data, user_id)

    @staticmethod
    def parse_dict(
        data: dict[str, Any],
        user_id: str | None = None,
    ) -> ComposeFile:
        """Parse a dictionary representation of a docker-compose file."""
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

        # Add user context metadata if provided
        if user_id:
            compose_file.user_id = user_id

        return compose_file

    @staticmethod
    def _parse_service(
        service_name: str,
        config: dict[str, Any],
        networks: list[ComposeNetwork],
        volumes: list[ComposeVolume],
    ) -> ComposeService:
        """Parse a single service configuration."""
        # Parse ports
        ports = []
        for port_config in config.get("ports", []):
            if isinstance(port_config, str):
                # Parse string format like "8080:80" or "80"
                port = ComposeParser._parse_port_string(port_config)
                if port:
                    ports.append(port)
            elif isinstance(port_config, dict):
                # Dict format with target/protocol
                ports.append(ComposePort(**port_config))
            elif isinstance(port_config, int):
                # Simple integer port
                ports.append(ComposePort(target=port_config))

        # Parse volumes
        service_volumes = []
        defined_volumes = [volume.name for volume in volumes]
        for volume_config in config.get("volumes", []):
            if isinstance(volume_config, str):
                # Parse string format like "myvolume:/data" or "/host/path:/container/path"
                volume = ComposeParser._parse_volume_string(volume_config)
                if volume:
                    # Only support named volumes that are defined
                    if (
                        volume.type == "volume"
                        and volume.source
                        and volume.source in [name for name in defined_volumes]
                    ):
                        # Only add if volume is defined, no bind mounts
                        service_volumes.append(volume)

            elif isinstance(volume_config, dict):
                # Dict format
                service_vol = ServiceVolume(**volume_config)
                # Only support named volumes that are defined
                if (
                    service_vol.type == "volume"
                    and service_vol.source
                    and service_vol.source in [name for name in defined_volumes]
                ):
                    service_volumes.append(service_vol)

        # Parse networks
        service_networks = []
        networks_config = config.get("networks")
        defined_networks = [network.name for network in networks]
        if networks_config:
            if isinstance(networks_config, list):
                # Simple list format
                for net in networks_config:
                    # Only add if network is defined
                    if networks and net in defined_networks:
                        service_networks.append(ServiceNetwork(name=net))
            elif isinstance(networks_config, dict):
                # Dict format with aliases
                for net_name, net_config in networks_config.items():
                    # Only add if network is defined
                    if networks and net_name in defined_networks:
                        if isinstance(net_config, dict):
                            service_networks.append(
                                ServiceNetwork(
                                    name=net_name, aliases=net_config.get("aliases")
                                )
                            )
                        else:
                            service_networks.append(ServiceNetwork(name=net_name))

        # Parse deploy config and scaling
        deploy = None
        scaling = None
        deploy_config = config.get("deploy")
        if deploy_config and isinstance(deploy_config, dict):
            # Only pass resources if it exists and is valid
            deploy_kwargs = {}
            if deploy_config.get("replicas") is not None:
                deploy_kwargs["replicas"] = deploy_config["replicas"]

            # Handle resources - ensure both limits and requests are dicts if resources exists
            resources = deploy_config.get("resources")
            if resources is not None and isinstance(resources, dict):
                cleaned_resources = {}

                # Use Pydantic to handle defaults and validation
                if "limits" in resources and resources["limits"]:
                    try:
                        cleaned_resources["limits"] = ResourceConfig(
                            **resources["limits"]
                        )
                    except Exception as e:
                        logger.warning(f"Invalid limits config for {service_name}: {e}")

                if "reservations" in resources and resources["reservations"]:
                    try:
                        cleaned_resources["reservations"] = ResourceConfig(
                            **resources["reservations"]
                        )
                    except Exception as e:
                        logger.warning(
                            f"Invalid reservations config for {service_name}: {e}"
                        )

                # Only add resources if at least one valid config exists
                if cleaned_resources:
                    deploy_kwargs["resources"] = ResourcesConfig(**cleaned_resources)

            deploy = DeployConfig(**deploy_kwargs) if deploy_kwargs else None

            # Parse scaling config from deploy labels
            labels = deploy_config.get("labels", {})
            if isinstance(labels, dict):
                # Check for LazyCloud scaling labels
                scaling_labels = {
                    k: v
                    for k, v in labels.items()
                    if k.startswith("lazycloud.scaling.")
                }
                if scaling_labels:
                    scaling = ScalingConfig(
                        enabled=scaling_labels.get(
                            "lazycloud.scaling.enabled", "false"
                        ).lower()
                        == "true",
                        min=int(scaling_labels.get("lazycloud.scaling.min", 1))
                        if "lazycloud.scaling.min" in scaling_labels
                        else None,
                        max=int(scaling_labels.get("lazycloud.scaling.max", 10))
                        if "lazycloud.scaling.max" in scaling_labels
                        else None,
                        cpu=scaling_labels.get("lazycloud.scaling.cpu"),
                        memory=scaling_labels.get("lazycloud.scaling.memory"),
                    )

        # Parse healthcheck
        healthcheck = None
        healthcheck_config = config.get("healthcheck")
        if healthcheck_config and isinstance(healthcheck_config, dict):
            healthcheck = HealthCheck(
                test=healthcheck_config.get("test"),
                interval=healthcheck_config.get("interval"),
                timeout=healthcheck_config.get("timeout"),
                retries=healthcheck_config.get("retries"),
                start_period=healthcheck_config.get("start_period"),
                disable=healthcheck_config.get("disable", False),
            )

        # Passe command. if it is a list, join it with spaces
        if isinstance(config.get("command"), list):
            config["command"] = " ".join(config["command"])

        return ComposeService(
            name=service_name,
            image=config.get("image"),
            command=config.get("command"),
            ports=ports if ports else None,
            volumes=service_volumes if service_volumes else None,
            networks=service_networks if service_networks else None,
            deploy=deploy,
            healthcheck=healthcheck,
            scaling=scaling,
        )

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
