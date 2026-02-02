import re
from collections.abc import Sequence

from models.compose import (
    ComposeFile,
    HealthCheck,
    ServiceVolume,
)
from models.helm import (
    HealthCheckValues,
    ProbeConfig,
    VolumeMount,
)
from models.k8s import ExecProbe, HttpGetProbe

from backend.services.k8s.generators.converters import parse_duration


def generate_service_volumes_values(
    volumes: Sequence[ServiceVolume], compose: ComposeFile
) -> list[VolumeMount]:
    """Generate Helm values for service volume mounts, only for defined volumes."""
    volumes_values: list[VolumeMount] = []
    defined_volumes = {v.name: v for v in (compose.volumes or [])}

    for volume in volumes:
        # Only process named volumes (not bind mounts)
        if volume.type != "volume" or not volume.source or not volume.target:
            continue

        # Only add volume mount if the volume is defined and not external
        if (
            volume.source in defined_volumes
            and not defined_volumes[volume.source].external
        ):
            volumes_values.append(
                VolumeMount(
                    name=volume.source,
                    mountPath=volume.target,
                    subPath=None,
                    readOnly=volume.read_only,
                    size="1Gi",
                )
            )

    return volumes_values


def generate_healthcheck_values(
    healthcheck: HealthCheck, service_name: str | None = None
) -> HealthCheckValues:
    """Generate Helm values for health checks.

    Args:
        healthcheck: Either a dict or a HealthCheck Pydantic model
        service_name: Name of the service for hostname conversion
    """
    healthcheck_values = HealthCheckValues(enabled=True)

    if not healthcheck:
        return healthcheck_values

    test_cmd = healthcheck.test
    if test_cmd and isinstance(test_cmd, list) and test_cmd[0] == "CMD":
        command = test_cmd[1:]

        # Try to detect HTTP health checks and convert them to httpGet probes
        probe = _parse_healthcheck_command(command, service_name)

        # Add timing configurations
        if healthcheck.interval:
            probe.period_seconds = parse_duration(healthcheck.interval)

        if healthcheck.timeout:
            probe.timeout_seconds = parse_duration(healthcheck.timeout)

        if healthcheck.start_period:
            probe.initial_delay_seconds = parse_duration(healthcheck.start_period)
        if healthcheck.retries:
            probe.failure_threshold = healthcheck.retries

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
                        httpGet.http_headers = [{"name": "Host", "value": host}]

                    return ProbeConfig(httpGet=httpGet)

    # Default to exec probe for other commands
    return ProbeConfig(exec=ExecProbe(command=command))
