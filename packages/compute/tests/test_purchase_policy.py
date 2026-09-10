from datetime import UTC, datetime
from decimal import Decimal

from compute.fleet_policy import FleetCapacityPolicy
from compute.offers import pooled_cloud_offer
from compute.purchase_policy import PurchaseRejection, assess_fleet_purchase
from shared.supplier_costs import SupplierCostTerms


def test_purchase_margin_counts_packed_resources_and_all_supplier_components() -> None:
    offer = pooled_cloud_offer(
        offer_id="node:spot:zone-a",
        provider="provider",
        cloud="cloud",
        instance_type="node",
        region="region",
        cpu_millicores=32_000,
        memory_mb=128 * 1024,
        preemptible=True,
        capability_key="node:spot:zone-a",
        cost_terms=SupplierCostTerms(
            compute_hourly_micros=641_853,
            root_disk_hourly_micros=22_223,
            public_ipv4_hourly_micros=5_000,
        ),
    )
    policy = FleetCapacityPolicy(minimum_purchase_margin_percent=50)
    now = datetime(2026, 9, 12, tzinfo=UTC)
    assessment = assess_fleet_purchase(offer, policy, preemptible=True, now=now)
    assert assessment.capacity_revenue_hourly_nanos == Decimal("1338153828.125")
    assert assessment.max_hourly_cost_micros == 669_076
    assert assessment.accepted

    expensive = offer.model_copy(
        update={
            "cost_terms": offer.cost_terms.model_copy(update={"compute_hourly_micros": 641_854})
        }
    )
    assert (
        assess_fleet_purchase(expensive, policy, preemptible=True, now=now).rejection
        is PurchaseRejection.InsufficientMargin
    )


def test_non_preemptible_purchase_uses_three_times_cpu_ram_but_the_same_gpu_rate() -> None:
    offer = pooled_cloud_offer(
        offer_id="node:on-demand",
        provider="provider",
        cloud="cloud",
        instance_type="node",
        region="region",
        cpu_millicores=32_000,
        memory_mb=128 * 1024,
        gpu="L4",
        gpu_count=1,
        capability_key="node:on-demand",
        cost_terms=SupplierCostTerms(
            compute_hourly_micros=1_200_000,
            root_disk_hourly_micros=0,
            public_ipv4_hourly_micros=0,
        ),
    )
    policy = FleetCapacityPolicy(minimum_purchase_margin_percent=50)
    now = datetime(2026, 9, 12, tzinfo=UTC)
    regular = assess_fleet_purchase(offer, policy, preemptible=False, now=now)
    assert regular.capacity_revenue_hourly_nanos == Decimal("4764461484.375")
    assert regular.accepted

    flexible = assess_fleet_purchase(offer, policy, preemptible=True, now=now)
    assert flexible.capacity_revenue_hourly_nanos == Decimal("2088153828.125")
    assert flexible.rejection is PurchaseRejection.InsufficientMargin
    spot = offer.model_copy(update={"preemptible": True})
    assert assess_fleet_purchase(spot, policy, preemptible=False, now=now) == flexible


def test_purchase_refuses_unknown_supplier_costs_and_unpriced_capacity() -> None:
    offer = pooled_cloud_offer(
        offer_id="node",
        provider="provider",
        cloud="cloud",
        instance_type="node",
        region="region",
        cpu_millicores=8_000,
        memory_mb=32 * 1024,
        capability_key="node",
        cost_terms=SupplierCostTerms(compute_hourly_micros=1),
    )
    policy = FleetCapacityPolicy()
    now = datetime(2026, 9, 12, tzinfo=UTC)
    assert (
        assess_fleet_purchase(offer, policy, preemptible=False, now=now).rejection
        is PurchaseRejection.UnknownCost
    )
    unpriced = offer.model_copy(
        update={
            "gpu": "unpriced",
            "gpu_count": 1,
            "cost_terms": SupplierCostTerms(
                compute_hourly_micros=1,
                root_disk_hourly_micros=0,
                public_ipv4_hourly_micros=0,
            ),
        }
    )
    assert (
        assess_fleet_purchase(unpriced, policy, preemptible=False, now=now).rejection
        is PurchaseRejection.UnpricedCapacity
    )
