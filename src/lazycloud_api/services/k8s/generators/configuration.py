import re
from typing import Any

from lazycloud_api.services.k8s.generators.converters import parse_duration
from shared.models.compose import (
    ComposeFile,
    ServiceVolume,
)
from shared.models.helm import (
    HealthCheckValues,
    ProbeConfig,
    VolumeMount,
)
from shared.models.k8s import ExecProbe, HttpGetProbe


def generate_service_volumes_values(
    volumes: list[str | ServiceVolume], compose: ComposeFile
) -> list[VolumeMount]:
    """Generate Helm values for service volume mounts, only for defined volumes."""
    volumes_values = []
    defined_volumes = {v.name: v for v in (compose.volumes or [])}

    for volume in volumes:
        volume_name = None
        mount_path = None
        read_only = False

        if isinstance(volume, str):
            # Parse volume string (volume_name:mount_path)
            if ":" in volume:
                parts = volume.split(":")
                if len(parts) >= 2 and not parts[0].startswith("/"):
                    # Named volume
                    volume_name = parts[0]
                    mount_path = parts[1]
                # Skip bind mounts (start with / or .). Local volumes are not supported.
        elif (
            isinstance(volume, ServiceVolume)
            and volume.type == "volume"
            and volume.source
        ):
            volume_name = volume.source
            mount_path = volume.target
            read_only = volume.read_only

        # Only add volume mount if the volume is defined in the volumes section
        if volume_name and mount_path:
            if (
                volume_name in defined_volumes
                and not defined_volumes[volume_name].external
            ):
                volumes_values.append(
                    VolumeMount(
                        name=volume_name,
                        mountPath=mount_path,
                        readOnly=read_only,
                        # default size required but ignored when we use EFS volumes
                        size="1Gi",
                    )
                )

    return volumes_values


def generate_healthcheck_values(
    healthcheck: dict[str, Any], service_name: str | None = None
) -> HealthCheckValues:
    """Generate Helm values for health checks."""
    healthcheck_values = HealthCheckValues(enabled=True)

    if "test" in healthcheck:
        test_cmd = healthcheck["test"]
        if isinstance(test_cmd, list) and test_cmd[0] == "CMD":
            command = test_cmd[1:]

            # Try to detect HTTP health checks and convert them to httpGet probes
            probe = _parse_healthcheck_command(command, service_name)

            # Add timing configurations
            if "interval" in healthcheck:
                probe.periodSeconds = parse_duration(healthcheck["interval"])

            if "timeout" in healthcheck:
                probe.timeoutSeconds = parse_duration(healthcheck["timeout"])

            if "start_period" in healthcheck:
                probe.initialDelaySeconds = parse_duration(healthcheck["start_period"])
            if "retries" in healthcheck:
                probe.failureThreshold = healthcheck["retries"]

            healthcheck_values.livenessProbe = probe
            healthcheck_values.readinessProbe = probe.model_copy()

    return healthcheck_values


def _parse_healthcheck_command(
    command: list[str], service_name: str | None = None
) -> ProbeConfig:
    """Parse healthcheck command and convert HTTP checks to httpGet probes."""
    # Check for curl commands
    if command and command[0] == "curl":
        # Look for URL in curl command
        url = None
        for i, arg in enumerate(command):
            if arg.startswith("http://") or arg.startswith("https://"):
                url = arg
                break
            elif i > 0 and not command[i - 1].startswith("-"):
                # Assume it's a URL if it's not preceded by a flag
                if not arg.startswith("-"):
                    url = arg
                    break

        if url:
            # Match http[s]://[host][:port][/path]
            match = re.match(r"(https?)://([^:/]+)(?::(\d+))?(/.*)?", url)
            if match:
                scheme = match.group(1)
                host = match.group(2)
                port = match.group(3)
                path = match.group(4) or "/"

                # For Kubernetes probes, if the hostname matches the current service name,
                # convert it to localhost since the probe runs inside the container
                if service_name and host == service_name:
                    host = "localhost"
                # Also convert IP addresses to localhost
                elif host == "127.0.0.1":
                    host = "localhost"

                httpGet = HttpGetProbe(
                    path=path,
                    port=int(port) if port else (443 if scheme == "https" else 80),
                )

                if scheme == "https":
                    httpGet.scheme = "HTTPS"

                # Only add host header if it's not localhost
                if host != "localhost":
                    httpGet.httpHeaders = [{"name": "Host", "value": host}]

                return ProbeConfig(httpGet=httpGet)

    # Check for wget commands
    elif command and command[0] == "wget":
        # Look for URL in wget command
        for arg in command:
            if arg.startswith("http://") or arg.startswith("https://"):
                match = re.match(r"(https?)://([^:/]+)(?::(\d+))?(/.*)?", arg)
                if match:
                    scheme = match.group(1)
                    host = match.group(2)
                    port = match.group(3)
                    path = match.group(4) or "/"

                    # Same logic as curl: convert matching service names to localhost
                    if service_name and host == service_name:
                        host = "localhost"
                    elif host == "127.0.0.1":
                        host = "localhost"

                    httpGet = HttpGetProbe(
                        path=path,
                        port=int(port) if port else (443 if scheme == "https" else 80),
                    )

                    if scheme == "https":
                        httpGet.scheme = "HTTPS"

                    if host != "localhost":
                        httpGet.httpHeaders = [{"name": "Host", "value": host}]

                    return ProbeConfig(httpGet=httpGet)

    # Default to exec probe for other commands
    return ProbeConfig(exec=ExecProbe(command=command))
