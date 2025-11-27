import re
from typing import Any

from lazycloud_api.services.k8s.generators.converters import (
    convert_cpu_value,
    convert_memory_value,
    is_kubernetes_name_compliant,
    sanitize_name,
)
from shared.models.compose import ComposeFile
from shared.models.validation import ValidationError
from shared.responses.deployments import ValidationResult


class ComposeValidator:
    def __init__(self):
        self.errors: list[ValidationError] = []
        self.warnings: list[ValidationError] = []

    def validate(
        self, compose: ComposeFile
    ) -> tuple[list[ValidationError], list[ValidationError]]:
        """
        Validate a compose file.
        """
        self.errors = []
        self.warnings = []

        # Run all validation checks
        self._validate_service_names(compose)
        self._validate_network_configuration(compose)
        self._validate_volumes(compose)
        self._validate_resources(compose)
        self._validate_ports(compose)
        self._validate_image_names(compose)
        self._validate_security_settings(compose)

        return self.errors, self.warnings

    def validate_to_result(self, compose: ComposeFile) -> ValidationResult:
        """
        Validate a compose file and return a ValidationResult.
        """
        errors, warnings = self.validate(compose)
        can_deploy = len(errors) == 0
        return ValidationResult(
            errors=[e.message for e in errors],
            warnings=[w.message for w in warnings],
            can_deploy=can_deploy,
        )

    def _validate_service_names(self, compose: ComposeFile):
        """Check that all service names are kubernetes compliant."""
        for service in compose.services:
            if not is_kubernetes_name_compliant(service.name):
                issues = []

                if service.name != service.name.lower():
                    issues.append("must be lowercase")

                if re.search(r"[^a-z0-9-]", service.name.lower()):
                    issues.append(
                        "can only contain lowercase letters, numbers, and hyphens"
                    )

                if service.name and service.name[0].isdigit():
                    issues.append("cannot start with a number")

                if service.name and service.name[0] == "-":
                    issues.append("cannot start with a hyphen")

                if service.name and service.name[-1] == "-":
                    issues.append("cannot end with a hyphen")

                if len(service.name) > 63:
                    issues.append(
                        f"must be 63 characters or less (currently {len(service.name)})"
                    )

                issue_text = (
                    "; ".join(issues) if issues else "does not meet naming requirements"
                )

                self.errors.append(
                    ValidationError(
                        error_type="invalid_name",
                        message=f"Service name '{service.name}' is not compliant: {issue_text}",
                        service=service.name,
                        suggestion="Use lowercase letters, numbers, and hyphens. "
                        "Must start with a letter and be 63 characters or less. "
                        f"For example: '{sanitize_name(service.name)}'",
                    )
                )

        # Also check for other resource names
        self._validate_resource_names("volume", compose.volumes)
        self._validate_resource_names("network", compose.networks)

    def _validate_resource_names(
        self, resource_type: str, resources: dict[str, Any] | None
    ):
        """Validate that resource names are kubernetes compliant."""
        if not resources:
            return

        for resource in resources:
            if not is_kubernetes_name_compliant(resource.name):
                sanitized = sanitize_name(resource.name)
                if sanitized and is_kubernetes_name_compliant(sanitized):
                    self.warnings.append(
                        ValidationError(
                            error_type="name_will_be_converted",
                            message=f"{resource_type.capitalize()} name '{resource.name}' will be converted to '{sanitized}'",
                            field=f"{resource_type}s",
                            suggestion=f"Consider renaming to '{sanitized}' in your compose file",
                        )
                    )

                else:
                    self.errors.append(
                        ValidationError(
                            error_type="invalid_name",
                            message=f"{resource_type.capitalize()} name '{resource.name}' cannot be made compliant",
                            field=f"{resource_type}s",
                            suggestion="Use lowercase letters, numbers, and hyphens only",
                        )
                    )

    def _validate_network_configuration(self, compose: ComposeFile):
        """Validate network configurations."""
        for service in compose.services:
            if service.networks and len(service.networks) > 1:
                self.warnings.append(
                    ValidationError(
                        error_type="multiple_networks_info",
                        message=f"Service uses multiple networks {service.networks}. "
                        f"Network isolation will be enforced via NetworkPolicies",
                        service=service.name,
                        field="networks",
                        suggestion="Enable networkPolicies to enforce network isolation",
                    )
                )

    def _validate_volumes(self, compose: ComposeFile):
        """Validate volume configurations."""
        defined_volumes = [volume.name for volume in compose.volumes]

        for service in compose.services:
            if not service.volumes:
                continue

            for volume in service.volumes:
                # Check for bind mounts - handle both string and ServiceVolume objects
                is_bind_mount = False
                volume_repr = str(volume)

                if hasattr(volume, "type") and volume.type == "bind":
                    # ServiceVolume object with type="bind"
                    is_bind_mount = True
                    volume_repr = f"{volume.source}:{volume.target}"

                elif isinstance(volume, str) and ":" in volume:
                    parts = volume.split(":")
                    if len(parts) >= 2 and (
                        parts[0].startswith("/") or parts[0].startswith(".")
                    ):
                        is_bind_mount = True

                if is_bind_mount:
                    self.warnings.append(
                        ValidationError(
                            error_type="bind_mount",
                            message=f"Local volume mount '{volume_repr}' are not supported",
                            service=service.name,
                            field="volumes",
                            suggestion="Use defined volumes instead",
                        )
                    )
                    continue

                # Check for undefined volumes
                if isinstance(volume, str) and ":" in volume:
                    volume_name = volume.split(":")[0]
                    if (
                        volume_name not in defined_volumes
                        and not volume_name.startswith(("/", ".", "~"))
                    ):
                        self.errors.append(
                            ValidationError(
                                error_type="undefined_volume",
                                message=f"Volume '{volume_name}' is not defined in the volumes section",
                                service=service.name,
                                field="volumes",
                                suggestion=f"Add '{volume_name}' to the top-level volumes section",
                            )
                        )

    def _validate_resources(self, compose: ComposeFile):
        """Validate resource specifications."""
        for service in compose.services:
            if not service.deploy or not service.deploy.resources:
                continue

            resources = service.deploy.resources

            # Check for limits without requests
            if resources.limits and not resources.reservations:
                self.warnings.append(
                    ValidationError(
                        error_type="auto_generated_requests",
                        message="Resource limits defined without requests (reservations). Requests will be auto-generated",
                        service=service.name,
                        field="deploy.resources",
                        suggestion="Consider adding 'reservations' to explicitly control resource requests. "
                        "Auto-generated values: CPU=50% of limit, Memory=80% of limit",
                    )
                )

            # Validate memory values
            for resource_type, resource_config in [
                ("limits", resources.limits),
                ("reservations", resources.reservations),
            ]:
                if resource_config and resource_config.memory:
                    try:
                        memory = resource_config.memory
                        # Try to convert to ensure it's valid
                        convert_memory_value(memory)
                    except Exception as e:
                        self.errors.append(
                            ValidationError(
                                error_type="invalid_memory",
                                message=f"Invalid memory value '{memory}': {str(e)}",
                                service=service.name,
                                field=f"deploy.resources.{resource_type}.memory",
                                suggestion="Use values like '512M', '1G', '2Gi'",
                            )
                        )

            # Validate CPU values
            for resource_type, resource_config in [
                ("limits", resources.limits),
                ("reservations", resources.reservations),
            ]:
                if resource_config and resource_config.cpus:
                    try:
                        cpus = resource_config.cpus
                        # Try to convert to ensure it's valid
                        convert_cpu_value(cpus)

                        # Check for reasonable values
                        # Convert millicores (e.g., "1000m") to cores
                        if isinstance(cpus, str) and cpus.endswith("m"):
                            cpu_float = float(cpus.rstrip("m")) / 1000
                        else:
                            cpu_float = float(cpus)
                        if cpu_float > 16:
                            self.warnings.append(
                                ValidationError(
                                    error_type="high_cpu",
                                    message=f"Very high CPU {resource_type} ({cpus})",
                                    service=service.name,
                                    field=f"deploy.resources.{resource_type}.cpus",
                                    suggestion="Ensure this CPU allocation is necessary",
                                )
                            )

                    except Exception as e:
                        self.errors.append(
                            ValidationError(
                                error_type="invalid_cpu",
                                message=f"Invalid CPU value '{cpus}': {str(e)}",
                                service=service.name,
                                field=f"deploy.resources.{resource_type}.cpus",
                                suggestion="Use decimal values like '0.5' or '2.0'",
                            )
                        )

    def _validate_ports(self, compose: ComposeFile):
        """Validate port configurations."""
        used_ports: dict[int, list[str]] = {}

        for service in compose.services:
            if not service.ports:
                continue

            for port in service.ports:
                host_port: str | None = None

                # Handle both ComposePort objects and string ports
                if hasattr(port, "published"):
                    # ComposePort object
                    host_port = str(port.published)
                else:
                    # String port format
                    port_str = str(port)
                    if ":" in port_str:
                        parts = port_str.split(":")
                        # Could be "8080:80" or "127.0.0.1:8080:80"
                        if len(parts) == 2:
                            host_port = parts[0]
                        elif len(parts) == 3:
                            host_port = parts[1]
                        else:
                            self.errors.append(
                                ValidationError(
                                    error_type="invalid_port",
                                    message=f"Invalid port format '{port_str}'",
                                    service=service.name,
                                    field="ports",
                                    suggestion="Use format 'host:container' or 'host:container/protocol'",
                                )
                            )
                            continue
                    else:
                        # Simple port string like "80"
                        host_port = port_str

                # Check for port conflicts
                if host_port:
                    try:
                        port_num = int(host_port)
                        if port_num in used_ports:
                            self.errors.append(
                                ValidationError(
                                    error_type="port_conflict",
                                    message=f"Port {port_num} already used by {used_ports[port_num]}",
                                    service=service.name,
                                    field="ports",
                                    suggestion="Change the host port binding to avoid conflict",
                                )
                            )
                        else:
                            used_ports[port_num] = [service.name]
                    except ValueError:
                        pass  # Not a simple port number

    def _validate_image_names(self, compose: ComposeFile):
        """Validate image names."""
        for service in compose.services:
            if not service.image:
                self.errors.append(
                    ValidationError(
                        error_type="missing_image",
                        message="No image specified",
                        service=service.name,
                        field="image",
                        suggestion="Specify an image for the service",
                    )
                )
                continue

    def _is_valid_label_key(self, key: str) -> bool:
        """Check if a label key is valid."""
        if "/" in key:
            prefix, name = key.split("/", 1)
            if not re.match(
                r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?(\.[a-z0-9]([-a-z0-9]*[a-z0-9])?)*$",
                prefix,
            ):
                return False

            return (
                re.match(r"^[a-zA-Z0-9]([-._a-zA-Z0-9]*[a-zA-Z0-9])?$", name)
                is not None
            )

        else:
            return (
                re.match(r"^[a-zA-Z0-9]([-._a-zA-Z0-9]*[a-zA-Z0-9])?$", key) is not None
            )

    def _validate_security_settings(self, compose: ComposeFile):
        """Validate security-related configurations."""
        # Currently no specific security validations needed
        # Security contexts are auto-generated by workloads.py
        _ = compose  # Unused for now, but may be used in future
