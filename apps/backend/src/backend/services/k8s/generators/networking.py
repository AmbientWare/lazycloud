"""
Networking-related functionality for Helm values generation.
Handles port parsing, ingress configuration, and network policies.
"""

import hashlib
import random
from urllib.parse import urlparse, urlunparse

import petname
from models.compose import ComposeFile, ComposePort, ComposeService
from models.helm import (
    IngressTLS,
    IngressValues,
    ParsedPort,
    PortConfig,
)
from responses.deployments import ServiceEndpoints

from backend.config import app_config

VALID_PROTOCOLS = ["tcp", "udp", "sctp"]

# Pattern to match .public suffix in hostnames for public URL transformation
# Example: api.public -> api-xxxxx.lazycloud.dev
# Note: For internal services, just use the service name directly (e.g., redis:6379)
# K8s DNS handles resolution automatically for services with expose/ports defined
PUBLIC_SUFFIX = ".public"


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
    service: ComposeService, deployment_id: str | None
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
        # Use placeholder for validation when deployment_id is None (new deployments)
        short_id = deployment_id[:5] if deployment_id else "xxxxx"
        hostname_prefix = (
            f"{service.name}-{short_id}"
            if service.name
            else generate_petname(deployment_id or service.name)
        )
        hostname = f"{hostname_prefix}.{app_config.BASE_DOMAIN}"

    ingress_config = IngressValues(
        enabled=True,
        className="nginx",
        hostname=hostname,
        tls=IngressTLS(enabled=False),  # Cloudflare handles TLS termination
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


def transform_service_url(
    value: str,
    service_names: set[str],
    deployment_id: str | None,
) -> str:
    """Transform .public suffixes in URL values.

    Transforms:
    - https://api.public -> https://api-{short_id}.{base_domain}
    - https://api.public:8080/v1 -> https://api-{short_id}.{base_domain}:8080/v1

    Note: For internal service communication, use the service name directly
    (e.g., redis:6379). K8s DNS handles resolution automatically.

    Args:
        value: The environment variable value to transform
        service_names: Set of valid service names in the compose file
        deployment_id: Deployment ID for generating public URLs

    Returns:
        Transformed value with .public replaced
    """
    if PUBLIC_SUFFIX not in value:
        return value

    # Try to parse as URL first
    try:
        parsed = urlparse(value)
        if parsed.scheme and parsed.netloc:
            # It's a full URL - transform the netloc (host:port)
            new_netloc = _transform_netloc(parsed.netloc, service_names, deployment_id)
            # Rebuild URL with transformed netloc
            return urlunparse(
                (
                    parsed.scheme,
                    new_netloc,
                    parsed.path,
                    parsed.params,
                    parsed.query,
                    parsed.fragment,
                )
            )
    except Exception:
        pass

    # Not a standard URL format, try direct host:port transformation
    # This handles cases like "api.public:8080" without a scheme
    return _transform_netloc(value, service_names, deployment_id)


def _transform_netloc(
    netloc: str,
    service_names: set[str],
    deployment_id: str | None,
) -> str:
    """Transform a netloc (host or host:port) with .public suffix.

    Args:
        netloc: The host or host:port string
        service_names: Set of valid service names
        deployment_id: Deployment ID for public URLs

    Returns:
        Transformed netloc
    """
    # Split host and port
    if ":" in netloc and not netloc.startswith("["):
        # Has port (and not IPv6)
        host, port = netloc.rsplit(":", 1)
        port_suffix = f":{port}"
    else:
        host = netloc
        port_suffix = ""

    # Handle .public suffix
    if host.endswith(PUBLIC_SUFFIX):
        service_name = host[: -len(PUBLIC_SUFFIX)]
        if service_name in service_names:
            # Generate public hostname
            short_id = deployment_id[:5] if deployment_id else "xxxxx"
            public_host = f"{service_name}-{short_id}.{app_config.BASE_DOMAIN}"
            return f"{public_host}{port_suffix}"
        # Service not found, return unchanged
        return netloc

    return netloc


def transform_environment_urls(
    environment: dict[str, str] | None,
    service_names: set[str],
    deployment_id: str | None,
) -> dict[str, str] | None:
    """Transform all .public URLs in environment variables.

    Args:
        environment: Dictionary of environment variables
        service_names: Set of valid service names in the compose file
        deployment_id: Deployment ID for generating public URLs

    Returns:
        Transformed environment dictionary
    """
    if not environment:
        return None

    transformed = {}
    for key, value in environment.items():
        transformed[key] = transform_service_url(value, service_names, deployment_id)

    return transformed


def compute_service_endpoints(
    compose_file: ComposeFile, deployment_id: str | None
) -> dict[str, ServiceEndpoints]:
    """Compute internal and public endpoints for all services.

    Args:
        compose_file: Parsed compose file with services
        deployment_id: Deployment ID (used for public URL generation)

    Returns:
        Dictionary mapping service names to their endpoints
    """
    endpoints: dict[str, ServiceEndpoints] = {}

    for service in compose_file.services:
        # Compute internal URL (Kubernetes DNS)
        # Use the first port's target (container port) or default to 80
        internal_port = 80
        if service.ports:
            first_port = service.ports[0]
            internal_port = int(first_port.target)

        internal_url = f"http://{service.name}:{internal_port}"

        # Compute public URL (only if service has ports exposed)
        public_url = None
        if service.ports:
            if service.domain:
                # Custom domain
                public_url = f"https://{service.domain}"
            else:
                # Auto-generated hostname
                short_id = deployment_id[:5] if deployment_id else "xxxxx"
                hostname = f"{service.name}-{short_id}.{app_config.BASE_DOMAIN}"
                public_url = f"https://{hostname}"

        endpoints[service.name] = ServiceEndpoints(
            internal=internal_url,
            public=public_url,
        )

    return endpoints
