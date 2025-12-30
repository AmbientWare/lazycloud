import re
from typing import Any

from loguru import logger
from models.compose import (
    ComposeFile,
    ComposeNetwork,
    ComposePort,
    ComposeService,
    ComposeVolume,
    DeployConfig,
    HealthCheck,
    LazyCloudLabel,
    ResourceConfig,
    ResourcesConfig,
    ScalingConfig,
    ServiceNetwork,
    ServiceVolume,
)
from models.k8s import RestartPolicy


def _get_int(scaling_labels, key, default=None):
    return int(scaling_labels[key]) if key in scaling_labels else default


def _should_skip_service(service_config: dict[str, Any]) -> bool:
    """Check if a service should be skipped from deployment"""
    labels = service_config.get("labels", {})
    if isinstance(labels, dict):
        skip_value = labels.get(LazyCloudLabel.IGNORE, "false")
        return str(skip_value).lower() == "true"
    return False


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
        volumes_dict = data.get("volumes", {})
        volumes = []
        if volumes_dict:
            for volume_name, volume_config in volumes_dict.items():
                # If volume_config is None (simple declaration), use just the name
                if volume_config is None:
                    volumes.append(ComposeParser._parse_volume_definition(volume_name))
                else:
                    # Add the name to the config dict for parsing
                    if isinstance(volume_config, dict):
                        volume_config["name"] = volume_name
                    volumes.append(
                        ComposeParser._parse_volume_definition(volume_config)
                    )

        # Parse networks
        networks = [
            ComposeParser._parse_network(network_config)
            for network_config in data.get("networks", [])
            if network_config is not None
        ]

        # Parse services (excluding those marked with lazycloud.ignore)
        services = [
            ComposeParser._parse_service(
                service_name, service_config, networks, volumes
            )
            for service_name, service_config in data.get("services", {}).items()
            if service_config is not None and not _should_skip_service(service_config)
        ]

        # Validate that at least one service is defined
        if not services:
            raise ValueError(
                "No services defined in compose file. "
                "Please add at least one service to deploy. "
                "Note: Services marked with 'lazycloud.ignore=true' are excluded."
            )

        # Filter networks and volumes to only include those used by non-skipped services
        used_networks = set()
        used_volumes = set()

        for service in services:
            if service.networks:
                used_networks.update(net.name for net in service.networks)
            if service.volumes:
                # Collect volume sources, ensuring deduplication
                for vol in service.volumes:
                    if vol.source:
                        used_volumes.add(vol.source)

        # Deduplicate: filter networks and volumes to only include those used by non-skipped services
        filtered_networks = [net for net in networks if net.name in used_networks]
        filtered_volumes = [vol for vol in volumes if vol.name in used_volumes]

        # Create ComposeFile with user context
        compose_file = ComposeFile(
            version=version,
            services=services,
            networks=filtered_networks,
            volumes=filtered_volumes,
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

        # Handle top-level restart field (maps to deploy.restart_policy)
        deploy_config = config.get("deploy", {})
        if "restart" in config and "restart_policy" not in deploy_config:
            # Map docker-compose restart values to deploy.restart_policy
            restart_value = config.get("restart", "").lower()
            if restart_value == "no":
                deploy_config = {**deploy_config, "restart_policy": {"condition": "no"}}

            elif restart_value == "always":
                deploy_config = {
                    **deploy_config,
                    "restart_policy": {"condition": "any"},
                }

            elif restart_value == "on-failure":
                deploy_config = {
                    **deploy_config,
                    "restart_policy": {"condition": "on-failure"},
                }

            elif restart_value == "unless-stopped":
                deploy_config = {
                    **deploy_config,
                    "restart_policy": {"condition": "any"},
                }

        deploy, scaling = ComposeParser._parse_deploy_and_scaling(
            deploy_config, service_name
        )

        healthcheck = ComposeParser._parse_healthcheck(config.get("healthcheck", {}))

        # Parse custom domain from labels (service labels or deploy.labels)
        domain = ComposeParser._parse_domain(config)

        # Parse stop_grace_period (duration string like "30s", "1m")
        stop_grace_period = ComposeParser._parse_duration_to_seconds(
            config.get("stop_grace_period")
        )

        # Parse entrypoint - keep as string or list
        entrypoint = config.get("entrypoint")

        # Parse command - convert list to string if needed
        command = config.get("command")
        if isinstance(command, list):
            command = " ".join(command)

        # Parse working_dir
        working_dir = config.get("working_dir")

        # If build is specified but no image, generate a default image name
        # The CLI will replace this with the actual built image before deployment
        image = config.get("image")
        build = config.get("build")
        if build and not image:
            image = f"{service_name}:latest"

        return ComposeService(
            name=service_name,
            image=image,
            build=build,
            entrypoint=entrypoint,
            command=command,
            working_dir=working_dir,
            stop_grace_period=stop_grace_period,
            ports=ports or None,
            volumes=service_volumes or None,
            networks=service_networks or None,
            deploy=deploy,
            healthcheck=healthcheck,
            scaling=scaling or ScalingConfig(),
            domain=domain,
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
                # Single port means both published and target are the same
                ports.append(ComposePort(published=port_config, target=port_config))
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

        # Parse restart policy
        restart_policy_config = deploy_config.get("restart_policy", {})
        if restart_policy_config:
            condition = restart_policy_config.get("condition", "any")
            # Map docker-compose restart policy conditions to RestartPolicy enum
            if condition == "no":
                deploy_kwargs["restart_policy"] = RestartPolicy.NEVER
            elif condition == "on-failure":
                deploy_kwargs["restart_policy"] = RestartPolicy.ON_FAILURE
            else:  # "any", "always", "unless-stopped" all map to ALWAYS
                deploy_kwargs["restart_policy"] = RestartPolicy.ALWAYS

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
            enabled=scaling_labels.get(LazyCloudLabel.SCALING_ENABLED, "false").lower()
            == "true",
            min=_get_int(scaling_labels, LazyCloudLabel.SCALING_MIN),
            max=_get_int(scaling_labels, LazyCloudLabel.SCALING_MAX),
            cpu=scaling_labels.get(LazyCloudLabel.SCALING_CPU),
            memory=scaling_labels.get(LazyCloudLabel.SCALING_MEMORY),
        )

    @staticmethod
    def _parse_duration_to_seconds(duration: str | None) -> int | None:
        """Parse Docker duration string to seconds (e.g. '30s', '1m', '1m30s', '2h')."""
        if not duration:
            return None

        total_seconds = 0
        remaining = duration.strip()

        # Pattern matches number followed by unit (h, m, s, ms, us, ns)
        pattern = re.compile(r"(\d+)(h|m|s|ms|us|ns)")

        while remaining:
            match = pattern.match(remaining)
            if not match:
                logger.warning(
                    f"Invalid duration format: {duration}. Expected format like '30s', '1m', '1m30s'. Ignoring."
                )
                return None

            value = int(match.group(1))
            unit = match.group(2)

            if unit == "h":
                total_seconds += value * 3600
            elif unit == "m":
                total_seconds += value * 60
            elif unit == "s":
                total_seconds += value
            elif unit == "ms":
                total_seconds += value // 1000
            elif unit == "us":
                pass  # Microseconds rounded to 0
            elif unit == "ns":
                pass  # Nanoseconds rounded to 0

            remaining = remaining[match.end() :]

        return total_seconds if total_seconds > 0 else None

    @staticmethod
    def _parse_domain(config: dict) -> str | None:
        """Parse custom domain from service labels."""
        # Check service-level labels first
        labels = config.get("labels", {})
        if isinstance(labels, dict):
            domain = labels.get(LazyCloudLabel.DOMAIN) or labels.get(
                LazyCloudLabel.INGRESS_DOMAIN
            )
            if domain:
                return domain

        # Check deploy.labels as fallback
        deploy = config.get("deploy", {})
        if isinstance(deploy, dict):
            deploy_labels = deploy.get("labels", {})
            if isinstance(deploy_labels, dict):
                domain = deploy_labels.get(LazyCloudLabel.DOMAIN) or deploy_labels.get(
                    LazyCloudLabel.INGRESS_DOMAIN
                )
                if domain:
                    return domain

        return None

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
                # Handle port ranges (e.g., "4510-4559")
                if "-" in parts[0]:
                    # For ranges, just use the first port
                    port_num = int(parts[0].split("-")[0])
                else:
                    port_num = int(parts[0])
                return ComposePort(
                    published=port_num, target=port_num, protocol=protocol
                )

            elif len(parts) >= 2:
                # Take the last two parts for host:target
                # This handles both "8080:80" and "127.0.0.1:8080:80"
                host_port = parts[-2] if len(parts) > 2 else parts[0]
                target_port = parts[-1]

                # Handle port ranges
                if "-" in host_port:
                    host_port = int(host_port.split("-")[0])
                else:
                    host_port = int(host_port)

                if "-" in target_port:
                    target_port = int(target_port.split("-")[0])
                else:
                    target_port = int(target_port)

                return ComposePort(
                    published=host_port, target=target_port, protocol=protocol
                )

            else:
                # Should never reach here
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
                labels=config.get("labels"),
                external=config.get("external", False),
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
