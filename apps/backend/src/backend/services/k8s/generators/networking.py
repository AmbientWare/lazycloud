"""
Networking-related functionality for Helm values generation.
Handles port parsing, ingress configuration, and network policies.
"""

import hashlib
import random

import petname
from models.compose import ComposePort, ComposeService
from models.helm import (
    IngressTLS,
    IngressValues,
    ParsedPort,
    PortConfig,
)

from backend.config import app_config

VALID_PROTOCOLS = ["tcp", "udp", "sctp"]


def parse_port_string(port_str: str) -> ParsedPort:
    """Parse a port string in various formats.

    Supports:
    - "80" - single port (host and container use same port)
    - "8080:80" - host:container mapping
    - "127.0.0.1:8080:80" - ip:host:container mapping
    - "8080-8090:80-90" - port ranges
    - "80/tcp", "80/udp", "80/sctp" - with protocol
    - "127.0.0.1:8080:80/tcp" - full format with protocol
    """
    # Extract protocol if present
    protocol = "tcp"
    if "/" in port_str:
        port_str, protocol = port_str.rsplit("/", 1)
        protocol = protocol.lower()

        # Validate protocol
        if protocol not in VALID_PROTOCOLS:
            raise ValueError(
                f"Invalid protocol '{protocol}'. Must be one of: {', '.join(VALID_PROTOCOLS)}"
            )

    # Split by colons
    parts = port_str.split(":")

    if len(parts) == 1:
        # Single port: "80" or "80-85"
        if "-" in parts[0]:
            # Port range
            start, end = parts[0].split("-", 1)
            return ParsedPort(
                published=f"{start}-{end}",
                target=f"{start}-{end}",
                protocol=protocol,
            )

        else:
            try:
                port_num = int(parts[0])
                return ParsedPort(
                    published=port_num, target=port_num, protocol=protocol
                )

            except ValueError:
                raise ValueError(f"Invalid port number: {parts[0]}")

    elif len(parts) == 2:
        # "8080:80" format
        published = parts[0]
        target = parts[1]

        # Handle port ranges
        if "-" in published or "-" in target:
            return ParsedPort(
                published=published,
                target=target,
                protocol=protocol,
            )

        else:
            try:
                return ParsedPort(
                    published=int(published),
                    target=int(target),
                    protocol=protocol,
                )

            except ValueError:
                raise ValueError(f"Invalid port numbers: {published}:{target}")

    elif len(parts) == 3:
        # "127.0.0.1:8080:80" format - IP:host:container
        # For Kubernetes, we ignore the IP binding
        published = parts[1]
        target = parts[2]

        # Handle port ranges
        if "-" in published or "-" in target:
            return ParsedPort(
                published=published,
                target=target,
                protocol=protocol,
                ip=parts[0],  # Store IP for reference
            )

        else:
            try:
                return ParsedPort(
                    published=int(published),
                    target=int(target),
                    protocol=protocol,
                    ip=parts[0],
                )

            except ValueError:
                raise ValueError(
                    f"Invalid port numbers: {parts[0]}:{published}:{target}"
                )

    else:
        raise ValueError(f"Invalid port format: {port_str}")


def generate_ports_values(ports: list[str | int | ComposePort]) -> list[PortConfig]:
    """Generate Helm values for ports."""
    ports_values = []
    for port in ports:
        if isinstance(port, (int, str)):
            port_config = parse_port_string(str(port))
            ports_values.append(
                PortConfig(
                    name=f"port-{port_config.target}",
                    port=port_config.published or port_config.target,
                    targetPort=port_config.target,
                    protocol=port_config.protocol.upper(),
                )
            )

        elif isinstance(port, ComposePort):
            ports_values.append(
                PortConfig(
                    name=f"port-{port.target}",
                    port=port.target,
                    targetPort=port.target,
                    protocol=port.protocol.upper(),
                )
            )
    return ports_values


def generate_ingress_values(
    service: ComposeService, deployment_id: str
) -> IngressValues | None:
    """Generate ingress configuration from service labels."""
    # No ports means no ingress
    if not service.ports:
        return None

    # Determine the hostname
    if service.domain:
        # Use custom domain directly
        hostname = service.domain
    else:
        # Generate hostname with deployment ID for DNS uniqueness
        # Format: {service_name}-{short_deployment_id}.{base_domain}
        short_id = deployment_id[:5]
        hostname_prefix = (
            f"{service.name}-{short_id}"
            if service.name
            else generate_petname(deployment_id)
        )
        hostname = f"{hostname_prefix}.{app_config.BASE_DOMAIN}"

    ingress_config = IngressValues(
        enabled=True,
        className="nginx",
        hostname=hostname,
        tls=IngressTLS(enabled=True),
        annotations={},
    )

    return ingress_config


def generate_petname(service_name: str) -> str:
    """Generate a unique petname for the service using the petname library."""
    try:
        # Use service name hash to make it deterministic
        hash_obj = hashlib.md5(service_name.encode())
        seed = int(hash_obj.hexdigest(), 16) % 1000000

        # Set random seed for deterministic results
        random.seed(seed)

        # Generate deterministic petname (2 words: adjective-animal)
        generated_name = petname.generate(words=2, separator="-")
        return generated_name or f"app-{service_name[:8]}"

    except (ImportError, AttributeError):
        return f"app-{service_name[:8]}"
