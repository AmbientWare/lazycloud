"""Hetzner Cloud cost modeling for LazyCloud.

Infrastructure: Hetzner CPX shared instances for compute.
Storage: JuiceFS Cloud (managed metadata) + Hetzner Object Storage (data).
Storage is metered separately from compute.
"""

from dataclasses import dataclass
from enum import StrEnum

from models.billing import HOURS_PER_MONTH, MeterNames, UsageUnits


class Tier(StrEnum):
    """Subscription tiers (feature gates + resource limits)."""

    DEVELOPER = "developer"  # $0/month
    PRO = "pro"  # $19/month
    SCALE = "scale"  # $49/month


SUBSCRIPTION_PRICES: dict[Tier, int] = {
    Tier.DEVELOPER: 0,
    Tier.PRO: 19,
    Tier.SCALE: 49,
}


@dataclass(frozen=True)
class HetznerInstance:
    """Hetzner CPX shared instance specification."""

    name: str
    vcpu: int
    memory_gb: int
    disk_gb: int
    monthly_price: float

    @property
    def hourly_price(self) -> float:
        return self.monthly_price / HOURS_PER_MONTH

    @property
    def cost_per_cpu_hour(self) -> float:
        return self.hourly_price / self.vcpu

    @property
    def cost_per_gb_hour(self) -> float:
        return self.hourly_price / self.memory_gb


# Hetzner CPX shared instances (Ashburn, VA)
CPX: dict[str, HetznerInstance] = {
    "cpx11": HetznerInstance("CPX11", 2, 2, 40, 5.59),
    "cpx21": HetznerInstance("CPX21", 3, 4, 80, 10.59),
    "cpx31": HetznerInstance("CPX31", 4, 8, 160, 18.59),
    "cpx41": HetznerInstance("CPX41", 8, 16, 240, 34.09),
    "cpx51": HetznerInstance("CPX51", 16, 32, 360, 67.59),
}

# Worker node type — single pool, all tiers packed together
WORKER_INSTANCE = CPX["cpx41"]

# Per-service minimums (enforced by platform)
MIN_CPU_PER_SERVICE = 0.25
MIN_MEMORY_PER_SERVICE = 0.5  # GB

# Per-service ephemeral storage (flat, from node local disk)
EPHEMERAL_LIMIT_GI = 20  # hard cap before eviction
EPHEMERAL_REQUEST_MI = 512  # scheduling reservation

# Storage costs (JuiceFS Cloud + Hetzner Object Storage)
JUICEFS_CLOUD_PER_GB_MONTH = 0.02  # managed metadata service
HETZNER_OBJECT_STORAGE_PER_GB_MONTH = 0.006  # data storage
STORAGE_COST_PER_GB_MONTH = (
    JUICEFS_CLOUD_PER_GB_MONTH + HETZNER_OBJECT_STORAGE_PER_GB_MONTH
)  # $0.026

# Per-tier limits (must match backend/billing/product_details/ and product.md)
TIER_LIMITS: dict[Tier, dict] = {
    Tier.DEVELOPER: {
        "max_services": 3,  # total services across all deployments
        "max_cpu": 1.0,  # per service
        "max_memory": 4,  # per service (GB)
        "max_volume_gb": 10,  # per service
        "max_replicas": 1,  # no auto-scaling
        "ephemeral_gi": EPHEMERAL_LIMIT_GI,
    },
    Tier.PRO: {
        "max_services": 25,  # total services across all deployments
        "max_cpu": 4.0,  # per service
        "max_memory": 16,  # per service (GB)
        "max_volume_gb": 50,  # per service
        "max_replicas": 2,  # auto-scaling up to 2x
        "ephemeral_gi": EPHEMERAL_LIMIT_GI,
    },
    Tier.SCALE: {
        "max_services": 999,  # unlimited (use high number for simulation)
        "max_cpu": 8.0,  # per service
        "max_memory": 32,  # per service (GB)
        "max_volume_gb": 100,  # per service
        "max_replicas": 5,  # auto-scaling up to 5x
        "ephemeral_gi": EPHEMERAL_LIMIT_GI,
    },
}

# Build cost (pass-through from Depot)
BUILD_COST_PER_MINUTE = 0.04

# Cloudflare cost per hostname
CLOUDFLARE_PER_HOSTNAME_MONTH = 0.10


@dataclass(frozen=True)
class OverheadFactors:
    """Conservative overhead for node utilization inefficiency."""

    # System pods, fragmentation, scheduling gaps
    node_utilization: float = 0.75  # 25% overhead (conservative)

    @property
    def cost_multiplier(self) -> float:
        return 1.0 / self.node_utilization


OVERHEAD = OverheadFactors()


def calculate_revenue(
    cpu_hours: float,
    memory_gb_hours: float,
    storage_gb: float = 0,
    build_minutes: float = 0,
    tier: Tier = Tier.DEVELOPER,
) -> float:
    """Calculate monthly revenue from usage.

    CPU, Memory, Storage, and Build Minutes are all metered.
    Subscription fee is added on top.
    """
    revenue = float(SUBSCRIPTION_PRICES[tier])

    revenue += cpu_hours * UsageUnits.get_price_dollars(MeterNames.CPU_USAGE)
    revenue += memory_gb_hours * UsageUnits.get_price_dollars(MeterNames.MEMORY_USAGE)
    revenue += build_minutes * UsageUnits.get_price_dollars(MeterNames.BUILD_MINUTES)
    revenue += storage_gb * UsageUnits.get_price_dollars(MeterNames.STORAGE_USAGE)

    return revenue


def calculate_cost(
    cpu_hours: float,
    memory_gb_hours: float,
    storage_gb: float = 0,
    build_minutes: float = 0,
    endpoints: int = 0,
    overhead: OverheadFactors | None = None,
) -> float:
    """Calculate infrastructure cost.

    Compute: CPX41 shared instances (single pool).
    Storage: JuiceFS Cloud + Hetzner Object Storage (decoupled from compute).
    """
    oh = overhead or OVERHEAD
    instance = WORKER_INSTANCE

    # Usable resources per node after overhead
    usable_cpu = instance.vcpu * oh.node_utilization
    usable_mem = instance.memory_gb * oh.node_utilization

    # Total resources consumed (convert hours to monthly averages)
    cpu_cores = cpu_hours / HOURS_PER_MONTH
    memory_gb = memory_gb_hours / HOURS_PER_MONTH

    # Nodes needed for compute
    nodes_for_cpu = cpu_cores / usable_cpu
    nodes_for_mem = memory_gb / usable_mem
    total_nodes = max(nodes_for_cpu, nodes_for_mem)

    compute_cost = total_nodes * instance.monthly_price

    # Storage cost (decoupled from compute — JuiceFS + object storage)
    storage_cost = storage_gb * STORAGE_COST_PER_GB_MONTH

    # Build cost (pass-through from Depot)
    build_cost = build_minutes * BUILD_COST_PER_MINUTE

    # Endpoint cost (Cloudflare)
    endpoint_cost = endpoints * CLOUDFLARE_PER_HOSTNAME_MONTH

    return compute_cost + storage_cost + build_cost + endpoint_cost
