"""Monte Carlo pricing simulation engine.

All analysis is derived from Monte Carlo sampling of customer distributions.
No fixed customer profiles - everything is probabilistic.

Updated for Hetzner CPX compute + JuiceFS Cloud storage (metered).
"""

from dataclasses import dataclass
from typing import Any

import numpy as np

from .costs import (
    HOURS_PER_MONTH,
    MIN_CPU_PER_SERVICE,
    MIN_MEMORY_PER_SERVICE,
    TIER_LIMITS,
    OverheadFactors,
    Tier,
    calculate_cost,
    calculate_revenue,
)


@dataclass
class TierDistribution:
    """Distribution parameters for a tier's customers.

    Each customer has N deployments, each with M services.
    Services per deployment is sampled separately.
    """

    tier: Tier
    deployments_min: int
    deployments_max: int
    services_per_deployment_min: int  # services within each deployment
    services_per_deployment_max: int
    cpu_min: float  # per service (>= MIN_CPU_PER_SERVICE)
    cpu_max: float  # per service
    builds_min: int
    builds_max: int
    storage_min: float  # GB per service (for services that use volumes)
    storage_max: float  # GB per service
    volume_probability: float = 0.6  # fraction of services that use a volume


# Distributions by tier - based on realistic indie/startup usage patterns
# Most PaaS customers run small hobby/side projects, not enterprise workloads
TIER_DISTRIBUTIONS: dict[Tier, TierDistribution] = {
    Tier.DEVELOPER: TierDistribution(
        tier=Tier.DEVELOPER,
        deployments_min=1,
        deployments_max=1,
        services_per_deployment_min=1,
        services_per_deployment_max=2,
        cpu_min=0.25,
        cpu_max=0.5,  # side projects, light usage
        builds_min=1,
        builds_max=10,
        storage_min=0.5,
        storage_max=2.0,
        volume_probability=0.3,
    ),
    Tier.PRO: TierDistribution(
        tier=Tier.PRO,
        deployments_min=1,
        deployments_max=3,  # 1-3 projects
        services_per_deployment_min=1,
        services_per_deployment_max=4,  # web + api + worker + db
        cpu_min=0.25,
        cpu_max=1.0,  # small production apps
        builds_min=5,
        builds_max=30,  # weekly deploys
        storage_min=1.0,
        storage_max=10.0,
        volume_probability=0.5,
    ),
    Tier.SCALE: TierDistribution(
        tier=Tier.SCALE,
        deployments_min=1,
        deployments_max=5,  # multiple projects/environments
        services_per_deployment_min=2,
        services_per_deployment_max=6,  # modest microservices
        cpu_min=0.25,
        cpu_max=2.0,  # growing apps, not enterprise
        builds_min=20,
        builds_max=60,  # active development
        storage_min=2.0,
        storage_max=20.0,
        volume_probability=0.6,
    ),
}

# Tier mix probabilities
TIER_MIX: dict[Tier, float] = {
    Tier.DEVELOPER: 0.70,
    Tier.PRO: 0.22,
    Tier.SCALE: 0.08,
}


def _percentiles(arr: np.ndarray) -> dict[str, float]:
    """Return standard percentile summary for an array."""
    return {
        "p10": float(np.percentile(arr, 10)),
        "p50": float(np.percentile(arr, 50)),
        "p90": float(np.percentile(arr, 90)),
        "mean": float(np.mean(arr)),
        "std": float(np.std(arr)),
    }


def sample_customer_metrics(
    tier: Tier,
    rng: np.random.Generator,
    distributions: dict[Tier, TierDistribution] | None = None,
    overhead: OverheadFactors | None = None,
) -> tuple[float, float]:
    """Sample a random customer and return (revenue, cost).

    Each customer has N deployments, each with M services.
    Total services capped by tier max_services limit.
    Storage is metered per GB-month, capped by tier max_volume_gb per service.
    Not all services use volumes — volume_probability controls this.
    Minimum compute enforced: 0.25 CPU, 0.5 GB per service.
    """
    dists = distributions or TIER_DISTRIBUTIONS
    dist = dists[tier]
    limits = TIER_LIMITS[tier]

    num_deployments = int(rng.integers(dist.deployments_min, dist.deployments_max + 1))

    # Total services across all deployments (capped by tier limit)
    num_services = 0
    for _ in range(num_deployments):
        num_services += int(
            rng.integers(
                dist.services_per_deployment_min,
                dist.services_per_deployment_max + 1,
            )
        )
    num_services = min(num_services, limits["max_services"])

    cpu_per_service = max(
        MIN_CPU_PER_SERVICE,
        rng.uniform(dist.cpu_min, dist.cpu_max),
    )
    cpu_per_service = min(cpu_per_service, limits["max_cpu"])

    # Memory ratio: skewed toward 1:3-1:4 (most web apps are memory-heavy)
    # Beta(3, 1.5) maps [0,1] -> mostly 0.5-1.0, then scale to [1, 4]
    memory_ratio = 1.0 + 3.0 * rng.beta(3, 1.5)
    memory_per_service = max(
        MIN_MEMORY_PER_SERVICE,
        cpu_per_service * memory_ratio,
    )
    memory_per_service = min(memory_per_service, limits["max_memory"])

    # Replicas (most services run at 1x, some scale up)
    max_replicas = limits["max_replicas"]
    if max_replicas > 1:
        # Most services don't auto-scale; ~20% use replicas
        if rng.random() < 0.2:
            avg_replicas = rng.uniform(1.0, float(max_replicas))
        else:
            avg_replicas = 1.0
    else:
        avg_replicas = 1.0

    builds = int(rng.integers(dist.builds_min, dist.builds_max + 1))
    endpoints = max(1, num_services // 3)

    total_cpu = num_services * cpu_per_service * avg_replicas
    total_memory = num_services * memory_per_service * avg_replicas

    # Storage: not all services use volumes (capped by tier limit per service)
    max_volume_gb = limits["max_volume_gb"]
    services_with_volumes = sum(
        1 for _ in range(num_services) if rng.random() < dist.volume_probability
    )
    if services_with_volumes > 0:
        storage_per_volume = min(
            rng.uniform(dist.storage_min, dist.storage_max),
            max_volume_gb,
        )
        total_storage_gb = services_with_volumes * storage_per_volume
    else:
        total_storage_gb = 0

    revenue = calculate_revenue(
        cpu_hours=total_cpu * HOURS_PER_MONTH,
        memory_gb_hours=total_memory * HOURS_PER_MONTH,
        storage_gb=total_storage_gb,
        build_minutes=builds * 5,  # avg 5 min per build
        tier=tier,
    )
    cost = calculate_cost(
        cpu_hours=total_cpu * HOURS_PER_MONTH,
        memory_gb_hours=total_memory * HOURS_PER_MONTH,
        storage_gb=total_storage_gb,
        build_minutes=builds * 5,
        endpoints=endpoints,
        overhead=overhead,
    )
    return revenue, cost


def monte_carlo(
    num_customers: int,
    num_simulations: int = 1000,
    tier_mix: dict[Tier, float] | None = None,
    distributions: dict[Tier, TierDistribution] | None = None,
    overhead: OverheadFactors | None = None,
    seed: int | None = None,
) -> dict[str, Any]:
    """Run Monte Carlo simulation of a customer portfolio.

    Works for any num_customers including 1 (average single customer).
    Returns percentile estimates (p10, p50, p90) for key metrics.
    """
    rng = np.random.default_rng(seed)
    mix = tier_mix or TIER_MIX

    tiers = list(mix.keys())
    probs = list(mix.values())

    revenues = np.zeros(num_simulations)
    costs = np.zeros(num_simulations)

    for i in range(num_simulations):
        customer_tiers = rng.choice(tiers, size=num_customers, p=probs)
        for tier_str in customer_tiers:
            rev, cost = sample_customer_metrics(
                Tier(str(tier_str)), rng, distributions, overhead
            )
            revenues[i] += rev
            costs[i] += cost

    profits = revenues - costs
    margins = np.where(revenues > 0, profits / revenues, 0)

    return {
        "customers": num_customers,
        "simulations": num_simulations,
        "revenue": _percentiles(revenues),
        "cost": _percentiles(costs),
        "profit": _percentiles(profits),
        "margin": _percentiles(margins),
        "arpu": _percentiles(revenues / num_customers) if num_customers > 0 else None,
    }


def monte_carlo_growth(
    initial: int = 50,
    months: int = 24,
    growth_rate: float = 0.10,
    churn_rate: float = 0.05,
    num_simulations: int = 500,
    tier_mix: dict[Tier, float] | None = None,
    distributions: dict[Tier, TierDistribution] | None = None,
    overhead: OverheadFactors | None = None,
    seed: int | None = None,
) -> list[dict[str, Any]]:
    """Simulate customer growth over time using Monte Carlo at each month.

    Returns a list of MC results per month with customer count.
    Uses fewer simulations per month for speed (500 default).
    """
    results = []
    customers = float(initial)

    for month in range(1, months + 1):
        n = int(customers)
        mc = monte_carlo(
            n, num_simulations, tier_mix, distributions, overhead, seed=seed
        )
        mc["month"] = month
        mc["customers"] = n
        results.append(mc)

        customers = customers * (1 + growth_rate - churn_rate)

    return results


def monte_carlo_breakeven(
    customer_counts: list[int] | None = None,
    num_simulations: int = 500,
    tier_mix: dict[Tier, float] | None = None,
    distributions: dict[Tier, TierDistribution] | None = None,
    overhead: OverheadFactors | None = None,
    seed: int | None = None,
) -> list[dict[str, Any]]:
    """Find break-even point using Monte Carlo at each scale.

    Returns MC results per customer count with probability of profitability.
    """
    if customer_counts is None:
        customer_counts = [
            10,
            25,
            50,
            75,
            100,
            150,
            200,
            300,
            500,
            750,
            1000,
        ]

    results = []
    for n in customer_counts:
        mc = monte_carlo(
            n, num_simulations, tier_mix, distributions, overhead, seed=seed
        )
        mc["prob_profitable"] = 1.0 - float(
            np.mean(np.array([1 if mc["profit"]["p50"] > 0 else 0]))
        )
        results.append(mc)

    return results


# --- Scenario Presets ---


@dataclass
class Scenario:
    """A named scenario with distributions and overhead."""

    name: str
    description: str
    distributions: dict[Tier, TierDistribution]
    overhead: OverheadFactors
    tier_mix: dict[Tier, float]


SCENARIOS: dict[str, Scenario] = {
    "worst": Scenario(
        name="Worst Case",
        description="Heavier usage, more paying customers, higher storage",
        distributions={
            Tier.DEVELOPER: TierDistribution(
                Tier.DEVELOPER,
                deployments_min=1,
                deployments_max=1,
                services_per_deployment_min=1,
                services_per_deployment_max=3,
                cpu_min=0.25,
                cpu_max=0.75,
                builds_min=5,
                builds_max=15,
                storage_min=1.0,
                storage_max=5.0,
                volume_probability=0.5,
            ),
            Tier.PRO: TierDistribution(
                Tier.PRO,
                deployments_min=2,
                deployments_max=5,
                services_per_deployment_min=2,
                services_per_deployment_max=5,
                cpu_min=0.5,
                cpu_max=1.5,
                builds_min=15,
                builds_max=50,
                storage_min=3.0,
                storage_max=15.0,
                volume_probability=0.6,
            ),
            Tier.SCALE: TierDistribution(
                Tier.SCALE,
                deployments_min=3,
                deployments_max=8,
                services_per_deployment_min=3,
                services_per_deployment_max=8,
                cpu_min=1.0,
                cpu_max=3.0,
                builds_min=40,
                builds_max=100,
                storage_min=5.0,
                storage_max=30.0,
                volume_probability=0.7,
            ),
        },
        overhead=OverheadFactors(node_utilization=0.65),  # poor packing
        tier_mix={Tier.DEVELOPER: 0.60, Tier.PRO: 0.28, Tier.SCALE: 0.12},
    ),
    "base": Scenario(
        name="Base Case",
        description="Realistic indie/startup usage patterns",
        distributions=TIER_DISTRIBUTIONS,
        overhead=OverheadFactors(node_utilization=0.75),
        tier_mix=TIER_MIX,
    ),
    "best": Scenario(
        name="Best Case",
        description="Light hobby usage, mostly free tier",
        distributions={
            Tier.DEVELOPER: TierDistribution(
                Tier.DEVELOPER,
                deployments_min=1,
                deployments_max=1,
                services_per_deployment_min=1,
                services_per_deployment_max=1,
                cpu_min=0.25,
                cpu_max=0.25,
                builds_min=1,
                builds_max=5,
                storage_min=0.5,
                storage_max=1.0,
                volume_probability=0.2,
            ),
            Tier.PRO: TierDistribution(
                Tier.PRO,
                deployments_min=1,
                deployments_max=2,
                services_per_deployment_min=1,
                services_per_deployment_max=3,
                cpu_min=0.25,
                cpu_max=0.5,
                builds_min=3,
                builds_max=15,
                storage_min=0.5,
                storage_max=3.0,
                volume_probability=0.4,
            ),
            Tier.SCALE: TierDistribution(
                Tier.SCALE,
                deployments_min=1,
                deployments_max=3,
                services_per_deployment_min=2,
                services_per_deployment_max=4,
                cpu_min=0.5,
                cpu_max=1.0,
                builds_min=10,
                builds_max=30,
                storage_min=1.0,
                storage_max=5.0,
                volume_probability=0.5,
            ),
        },
        overhead=OverheadFactors(node_utilization=0.85),  # good packing
        tier_mix={Tier.DEVELOPER: 0.80, Tier.PRO: 0.15, Tier.SCALE: 0.05},
    ),
}
