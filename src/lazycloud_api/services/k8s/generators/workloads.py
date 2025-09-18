"""
Workload-related functionality for Helm values generation.
Handles image parsing, workload type detection, and resource management.
"""

from lazycloud_api.services.k8s.generators.converters import (
    convert_cpu_value,
    convert_memory_value,
)
from shared.models.compose import ResourcesConfig
from shared.models.helm import (
    ImageConfig,
    PodSecurityContext,
    SecurityContext,
)
from shared.models.k8s import ResourceRequirements, Resources, SecurityCapabilities


def parse_image(image_string: str) -> ImageConfig:
    """Parse image string into repository and tag with smart pull policy."""
    if ":" in image_string:
        repository, tag = image_string.rsplit(":", 1)

    else:
        repository = image_string
        tag = "latest"

    # Smart pull policy based on tag
    pull_policy = determine_pull_policy(tag)
    return ImageConfig(repository=repository, tag=tag, pullPolicy=pull_policy)


def determine_pull_policy(tag: str) -> str:
    """Determine appropriate pull policy based on image tag."""
    tag_lower = tag.lower()

    # Always pull for these common environment tags
    always_pull_tags = {"latest", "develop", "production", "staging"}

    if tag_lower in always_pull_tags:
        return "Always"

    # Default to IfNotPresent for all other tags
    return "IfNotPresent"


def should_be_statefulset(image: str, service_name: str) -> bool:
    """Determine if service should be deployed as StatefulSet instead of Deployment."""
    # Auto-detect based on image name (common databases)
    stateful_images = {
        "postgres",
        "postgresql",
        "mysql",
        "mariadb",
        "mongo",
        "mongodb",
        "redis",
        "elasticsearch",
        "cassandra",
        "neo4j",
        "influxdb",
        "etcd",
        "zookeeper",
        "kafka",
        "rabbitmq",
        "consul",
    }

    # Check image name
    image_lower = image.lower()
    if any(db in image_lower for db in stateful_images):
        return True

    # Check service name patterns
    service_name_lower = service_name.lower()
    return any(db in service_name_lower for db in stateful_images)


def generate_resources_values(
    resources_config: ResourcesConfig,
) -> Resources | None:
    """Generate Kubernetes resource constraints from Docker Compose deploy.resources."""
    resources = Resources()

    # Handle limits
    if resources_config.limits:
        limits = ResourceRequirements()

        if resources_config.limits.cpus:
            limits.cpu = convert_cpu_value(resources_config.limits.cpus)
        if resources_config.limits.memory:
            limits.memory = convert_memory_value(resources_config.limits.memory)

        if limits.cpu or limits.memory:
            resources.limits = limits

    # Handle requests (from "reservations" in Docker Compose)
    if resources_config.reservations:
        requests = ResourceRequirements()

        if resources_config.reservations.cpus:
            requests.cpu = convert_cpu_value(resources_config.reservations.cpus)
        if resources_config.reservations.memory:
            requests.memory = convert_memory_value(resources_config.reservations.memory)

        if requests.cpu or requests.memory:
            resources.requests = requests

    # Auto-generate requests if only limits are specified
    elif resources.limits:
        requests = ResourceRequirements()

        # CPU: Default to 50% of limit
        if resources.limits.cpu:
            cpu_limit = resources.limits.cpu
            # Parse CPU value (could be "1", "1.5", "1000m", etc.)
            if cpu_limit.endswith("m"):
                # Millicores
                cpu_limit_value = float(cpu_limit[:-1])
                requests.cpu = f"{int(cpu_limit_value * 0.5)}m"
            else:
                # Cores
                cpu_limit_value = float(cpu_limit)
                requests.cpu = str(cpu_limit_value * 0.5)

        # Memory: Default to 80% of limit
        if resources.limits.memory:
            memory_limit = resources.limits.memory
            # Parse memory value to calculate percentage
            requests.memory = _calculate_memory_request(memory_limit, 0.8)

        if requests.cpu or requests.memory:
            resources.requests = requests

    return resources if resources.limits or resources.requests else None


def generate_security_context_values() -> SecurityContext:
    """Generate security context based on image and port requirements"""
    # Default context - permissive since we're using gVisor for isolation
    security_context = SecurityContext(
        runAsNonRoot=False,  # Allow root
        runAsUser=0,  # Run as root by default
        runAsGroup=0,
        readOnlyRootFilesystem=False,  # Allow writes for compatibility
        allowPrivilegeEscalation=True,  # Allow escalation - gVisor handles security
        capabilities=SecurityCapabilities(
            drop=[]  # Keep all capabilities - gVisor provides isolation
        ),
    )

    # With gVisor, we don't need special handling for different images
    # gVisor provides the security isolation, so containers can run however they need to

    return security_context


def generate_pod_security_context_values(
    has_volumes: bool = False,
) -> PodSecurityContext:
    """Generate pod-level security context"""
    # Minimal pod security context - let gVisor handle security
    pod_security_context = PodSecurityContext()

    # Only set fsGroup if volumes are present
    if has_volumes:
        pod_security_context.fs_group = 0  # Root group for maximum compatibility

    return pod_security_context


def _calculate_memory_request(memory_limit: str, percentage: float = 0.8) -> str:
    """Calculate memory request based on memory limit"""
    if memory_limit.endswith("Gi"):
        value = float(memory_limit[:-2])
        return f"{value * percentage:.1f}Gi"

    elif memory_limit.endswith("G"):
        value = float(memory_limit[:-1])
        return f"{value * percentage:.1f}G"

    elif memory_limit.endswith("Mi"):
        value = float(memory_limit[:-2])
        return f"{int(value * percentage)}Mi"

    elif memory_limit.endswith("M"):
        value = float(memory_limit[:-1])
        return f"{int(value * percentage)}M"

    elif memory_limit.endswith("Ki"):
        value = float(memory_limit[:-2])
        return f"{int(value * percentage)}Ki"

    elif memory_limit.endswith("K"):
        value = float(memory_limit[:-1])
        return f"{int(value * percentage)}K"

    else:
        # Assume bytes
        try:
            value = int(memory_limit)
            return str(int(value * percentage))
        except ValueError:
            # If we can't parse it, return a conservative default
            return "128Mi"
