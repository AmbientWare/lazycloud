from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_FLOOR, Decimal
from enum import StrEnum

from shared.billing_rate_card import published_metered_rate_card
from shared.container_requests import billable_memory_capacity, schedulable_capacity
from shared.gpu import NO_GPU, normalize_gpu_type
from shared.placement import placement_rate_class
from shared.usage import UsageBillingOwner

from compute.fleet_policy import FleetCapacityPolicy
from compute.offers import ComputeOffer


class PurchaseRejection(StrEnum):
    UnknownCost = "supplier hourly cost is incomplete"
    UnpricedCapacity = "capacity has no applicable customer rate"
    InsufficientMargin = "supplier cost exceeds the fleet purchase margin limit"


@dataclass(frozen=True, slots=True)
class FleetPurchaseAssessment:
    rejection: PurchaseRejection | None
    hourly_cost_micros: int | None
    max_hourly_cost_micros: int | None
    capacity_revenue_hourly_nanos: Decimal | None

    @property
    def accepted(self) -> bool:
        return self.rejection is None


def assess_fleet_purchase(
    offer: ComputeOffer,
    policy: FleetCapacityPolicy,
    *,
    preemptible: bool,
    now: datetime,
) -> FleetPurchaseAssessment:
    cost = offer.cost_terms.complete_hourly_cost_micros
    if cost is None:
        return FleetPurchaseAssessment(PurchaseRejection.UnknownCost, None, None, None)
    # A Spot-tolerant workload keeps its lower rate on an On-Demand worker.
    rate_class = placement_rate_class(pinned=False, preemptible=preemptible or offer.preemptible)
    gpu_type = normalize_gpu_type(offer.gpu or NO_GPU)
    rate = next(
        (
            rate
            for rate in published_metered_rate_card(now).compute_rates
            if rate.billing_owner is UsageBillingOwner.PlatformFleet
            and rate.rate_class == rate_class
            and rate.gpu_type == gpu_type
        ),
        None,
    )
    if rate is None or offer.cpu_millicores <= 0 or offer.memory_mb <= 0:
        return FleetPurchaseAssessment(PurchaseRejection.UnpricedCapacity, cost, None, None)
    if bool(gpu_type) != (offer.gpu_count > 0):
        return FleetPurchaseAssessment(PurchaseRejection.UnpricedCapacity, cost, None, None)
    revenue = (
        Decimal(schedulable_capacity(offer.cpu_millicores)) * rate.nanos_per_cpu_core_hour / 1000
        + Decimal(billable_memory_capacity(schedulable_capacity(offer.memory_mb)))
        * rate.nanos_per_memory_gib_hour
        / 1024
        + offer.gpu_count * rate.nanos_per_gpu_card_hour
    )
    if revenue <= 0:
        return FleetPurchaseAssessment(PurchaseRejection.UnpricedCapacity, cost, None, revenue)
    ceiling = int(
        (revenue * (100 - policy.minimum_purchase_margin_percent) / 100_000).to_integral_value(
            rounding=ROUND_FLOOR
        )
    )
    return FleetPurchaseAssessment(
        PurchaseRejection.InsufficientMargin if cost > ceiling else None,
        cost,
        ceiling,
        revenue,
    )
