from models.compose import ResourcesConfig
from models.helm import (
    ImageConfig,
    PodSecurityContext,
    SecurityContext,
)
from models.k8s import ResourceRequirements, Resources, SecurityCapabilities

from backend.services.k8s.generators.converters import (
    convert_cpu_value,
    convert_memory_value,
    parse_cpu_to_cores,
    parse_memory_to_gb,
)

MIN_CPU_REQUEST_MILLICORES = 250
MIN_MEMORY_REQUEST_MI = 256


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
    # Check if reservations has actual values (not just an empty ResourceConfig)
    has_reservations = resources_config.reservations and (
        resources_config.reservations.cpus or resources_config.reservations.memory
    )
    if has_reservations:
        requests = ResourceRequirements()

        if resources_config.reservations.cpus:
            requests.cpu = convert_cpu_value(resources_config.reservations.cpus)
        if resources_config.reservations.memory:
            requests.memory = convert_memory_value(resources_config.reservations.memory)

        if requests.cpu or requests.memory:
            resources.requests = requests

    # Auto-generate requests if only limits are specified (no explicit reservations)
    elif resources.limits:
        requests = ResourceRequirements()

        if resources.limits.cpu:
            cpu_limit = resources.limits.cpu
            if cpu_limit.endswith("m"):
                cpu_limit_value = float(cpu_limit[:-1])
                requests.cpu = f"{int(cpu_limit_value * 0.5)}m"
            else:
                cpu_limit_value = float(cpu_limit)
                requests.cpu = str(cpu_limit_value * 0.5)

        if resources.limits.memory:
            memory_limit = resources.limits.memory
            requests.memory = _calculate_memory_request(memory_limit, 0.8)

        if requests.cpu or requests.memory:
            resources.requests = requests

    # Enforce minimum requests
    if resources.requests:
        resources.requests.cpu = _enforce_min_cpu(resources.requests.cpu)
        resources.requests.memory = _enforce_min_memory(resources.requests.memory)

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
            return "128Mi"


def _enforce_min_cpu(cpu_value: str | None) -> str:
    """Ensure CPU request meets minimum threshold."""
    if not cpu_value:
        return f"{MIN_CPU_REQUEST_MILLICORES}m"

    cores = parse_cpu_to_cores(cpu_value)
    millicores = int(cores * 1000)
    return f"{max(millicores, MIN_CPU_REQUEST_MILLICORES)}m"


def _enforce_min_memory(memory_value: str | None) -> str:
    """Ensure memory request meets minimum threshold."""
    if not memory_value:
        return f"{MIN_MEMORY_REQUEST_MI}Mi"

    gb = parse_memory_to_gb(memory_value)
    mi_value = int(gb * 1024)
    return f"{max(mi_value, MIN_MEMORY_REQUEST_MI)}Mi"
