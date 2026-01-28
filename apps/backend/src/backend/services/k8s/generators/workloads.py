from models.compose import ResourcesConfig
from models.helm import (
    ImageConfig,
    PodSecurityContext,
    SecurityContext,
)
from models.k8s import ResourceRequirements, Resources, SecurityCapabilities

from backend.services.k8s.generators.converters import (
    parse_cpu_to_cores,
    parse_memory_to_gb,
)

# Resource constraints
# Minimum: 0.25 CPU, 0.5 GB (1:2 ratio)
MIN_CPU_CORES = 0.25
MIN_MEMORY_GB = 0.5

# Ratio constraints: memory must be 1-4x CPU (in GB per core)
MIN_MEMORY_RATIO = 1  # 1 GB per CPU core minimum
MAX_MEMORY_RATIO = 4  # 4 GB per CPU core maximum

# Limits are 4x requests to allow bursting
LIMITS_MULTIPLIER = 4


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
    resources_config: ResourcesConfig | None = None,
) -> Resources:
    """Generate Kubernetes resource constraints from Docker Compose deploy.resources.

    Enforces:
    - Minimum: 0.25 CPU, 1 GB memory
    - CPU:Memory ratio between 1:1 and 1:4 (1-4 GB per CPU core)
    - Limits = 4x requests (allows bursting, user pays for max(used, requests))
    """
    # Parse user-specified values (prefer reservations over limits for requests)
    cpu_cores: float | None = None
    memory_gb: float | None = None

    if resources_config:
        # First check reservations (Docker Compose "reservations" = K8s "requests")
        if resources_config.reservations:
            if resources_config.reservations.cpus:
                cpu_cores = parse_cpu_to_cores(resources_config.reservations.cpus)
            if resources_config.reservations.memory:
                memory_gb = parse_memory_to_gb(resources_config.reservations.memory)

        # Fall back to limits if no reservations specified
        if (
            cpu_cores is None
            and resources_config.limits
            and resources_config.limits.cpus
        ):
            cpu_cores = parse_cpu_to_cores(resources_config.limits.cpus)
        if (
            memory_gb is None
            and resources_config.limits
            and resources_config.limits.memory
        ):
            memory_gb = parse_memory_to_gb(resources_config.limits.memory)

    # Normalize and enforce ratio constraints
    cpu_cores, memory_gb = _normalize_resources(cpu_cores, memory_gb)

    # Build requests
    requests = ResourceRequirements(
        cpu=_format_cpu(cpu_cores),
        memory=_format_memory(memory_gb),
    )

    # Build limits (4x requests for bursting)
    limits = ResourceRequirements(
        cpu=_format_cpu(cpu_cores * LIMITS_MULTIPLIER),
        memory=_format_memory(memory_gb * LIMITS_MULTIPLIER),
    )

    return Resources(requests=requests, limits=limits)


def _normalize_resources(
    cpu_cores: float | None, memory_gb: float | None
) -> tuple[float, float]:
    """Normalize CPU and memory to enforce ratio and minimum constraints.

    Rules:
    - If only CPU specified: memory = cpu * MAX_MEMORY_RATIO (1:4)
    - If only memory specified: cpu = memory / MAX_MEMORY_RATIO (1:4)
    - If both specified: enforce ratio bounds (1:1 to 1:4)
    - Enforce minimums after ratio adjustment
    """
    if cpu_cores is not None and memory_gb is not None:
        # Both specified - enforce ratio bounds
        ratio = memory_gb / cpu_cores if cpu_cores > 0 else MAX_MEMORY_RATIO

        if ratio < MIN_MEMORY_RATIO:
            # Too CPU-heavy, bump memory to 1:1
            memory_gb = cpu_cores * MIN_MEMORY_RATIO
        elif ratio > MAX_MEMORY_RATIO:
            # Too memory-heavy, bump CPU to 1:4
            cpu_cores = memory_gb / MAX_MEMORY_RATIO

    elif cpu_cores is not None:
        # Only CPU specified - default to 1:4 ratio
        memory_gb = cpu_cores * MAX_MEMORY_RATIO

    elif memory_gb is not None:
        # Only memory specified - default to 1:4 ratio
        cpu_cores = memory_gb / MAX_MEMORY_RATIO

    else:
        # Neither specified - use minimums
        cpu_cores = MIN_CPU_CORES
        memory_gb = MIN_MEMORY_GB

    # Enforce minimums
    cpu_cores = max(cpu_cores, MIN_CPU_CORES)
    memory_gb = max(memory_gb, MIN_MEMORY_GB)

    # Re-check ratio after minimums (minimums are 1:4, so should be fine)
    # But if user specified very low values, we might need to adjust
    ratio = memory_gb / cpu_cores
    if ratio < MIN_MEMORY_RATIO:
        memory_gb = cpu_cores * MIN_MEMORY_RATIO
    elif ratio > MAX_MEMORY_RATIO:
        cpu_cores = memory_gb / MAX_MEMORY_RATIO

    return cpu_cores, memory_gb


def _format_cpu(cores: float) -> str:
    """Format CPU cores as Kubernetes string (millicores)."""
    millicores = int(cores * 1000)
    return f"{millicores}m"


def _format_memory(gb: float) -> str:
    """Format memory GB as Kubernetes string (Mi or Gi)."""
    if gb >= 1.0:
        # Use Gi for >= 1 GB
        if gb == int(gb):
            return f"{int(gb)}Gi"
        return f"{gb:.1f}Gi"
    else:
        # Use Mi for < 1 GB
        mi = int(gb * 1024)
        return f"{mi}Mi"


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
