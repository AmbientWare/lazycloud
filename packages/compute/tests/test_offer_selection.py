from __future__ import annotations

import pytest
from compute.offers import (
    ComputeOffer,
    OfferRequest,
    choose_offer,
    filter_offers,
    pooled_cloud_offer,
)
from compute.providers import ProviderCapacityPolicy, ProviderOfferEligibility
from shared.container_requests import OciRuntimeName
from shared.supplier_costs import SupplierCostTerms


def _offer(instance_type: str, cpu_millicores: int, memory_mb: int) -> ComputeOffer:
    return pooled_cloud_offer(
        offer_id=f"us-east-1:{instance_type}",
        provider="aws",
        cloud="aws",
        instance_type=instance_type,
        region="us-east-1",
        cpu_millicores=cpu_millicores,
        memory_mb=memory_mb,
        cost_terms=SupplierCostTerms(
            compute_hourly_micros=100_000, root_disk_hourly_micros=0, public_ipv4_hourly_micros=0
        ),
        capability_key=f"aws:us-east-1:{instance_type}:amd64:runsc",
    )


def test_a_node_the_request_would_exactly_fill_is_not_offered() -> None:
    """A container needs a node with room left over for what starts it.

    The agent and the container runtime are already on the machine, so a node
    sized to the request exactly has nothing left to run the container with.
    """
    request = OfferRequest(
        regions=["us-east-1"],
        min_cpu_millicores=2_000,
        min_memory_mb=4 * 1024,
        architecture="amd64",
        runtime="runsc",
        nodes=1,
    )

    exact = _offer("c7i.large", 2_000, 4 * 1024)
    larger = _offer("c7i.xlarge", 4_000, 8 * 1024)

    assert filter_offers([exact], request) == []
    assert filter_offers([exact, larger], request) == [larger]


def test_purchase_respects_interruption_and_zone_requirements() -> None:
    regular = _offer("m7i.2xlarge", 8_000, 32 * 1024).model_copy(
        update={"availability_zone": "use1-az1"}
    )
    spot = regular.model_copy(update={"preemptible": True})
    assert filter_offers([spot, regular], OfferRequest()) == [regular]
    assert filter_offers([spot, regular], OfferRequest(preemptible=True)) == [spot, regular]
    assert (
        filter_offers([spot, regular], OfferRequest(preemptible=True, availability_zone="use1-az2"))
        == []
    )


def _gpu_offer(instance_type: str, gpu: str, hourly_cost_micros: int) -> ComputeOffer:
    return pooled_cloud_offer(
        offer_id=f"us-east-1:{instance_type}",
        provider="aws",
        cloud="aws",
        instance_type=instance_type,
        region="us-east-1",
        cpu_millicores=8_000,
        memory_mb=32 * 1024,
        cost_terms=SupplierCostTerms(
            compute_hourly_micros=hourly_cost_micros,
            root_disk_hourly_micros=0,
            public_ipv4_hourly_micros=0,
        ),
        capability_key=f"aws:us-east-1:{instance_type}:amd64:runsc",
        gpu=gpu,
        gpu_count=1,
    )


def _gpu_request(gpu: list[str]) -> OfferRequest:
    return OfferRequest(
        regions=["us-east-1"],
        min_cpu_millicores=1_000,
        min_memory_mb=1024,
        architecture="amd64",
        runtime=OciRuntimeName.Runsc.value,
        gpu=gpu,
        min_gpu_count=1,
        nodes=1,
    )


def test_buying_a_first_gpu_node_accepts_the_spellings_matching_a_pool_accepts() -> None:
    """The two halves of placement have to agree about a GPU name.

    Matching an existing pool normalised and honoured `any`; choosing an offer
    to buy compared raw strings and did neither. So `gpu="any"`, and every
    documented lowercase spelling, found a pool that already existed and could
    not create the first one, which surfaced as `offer_unavailable` to whoever
    happened to be first.
    """
    offers = [_gpu_offer("g6.xlarge", "L4", 800_000)]

    assert filter_offers(offers, _gpu_request(["any"])) == offers
    assert filter_offers(offers, _gpu_request(["l4"])) == offers
    assert filter_offers(offers, _gpu_request(["h100"])) == []


def test_the_order_an_author_wrote_outranks_the_cheaper_card() -> None:
    """A chain is an instruction, not a set to be re-sorted by price.

    Ranked by cost first, `["h100", "l4"]` would always take the L4 and the
    preference would mean nothing. The platform models no throughput, so it
    cannot judge that a card at five times the price is worth it, and does not
    substitute its own answer for the one the author gave.
    """
    preferred = _gpu_offer("p5.4xlarge", "H100", 6_880_000)
    cheaper = _gpu_offer("g6.xlarge", "L4", 800_000)

    assert choose_offer([cheaper, preferred], _gpu_request(["h100", "l4"])) is preferred
    # Cost still decides among cards the author ranked equally.
    assert choose_offer([cheaper, preferred], _gpu_request(["any"])) is cheaper


def test_an_unknown_supplier_price_does_not_outrank_known_costs() -> None:
    known = _offer("known", 4_000, 8 * 1024)
    unknown = known.model_copy(update={"cost_terms": SupplierCostTerms()})

    assert choose_offer([unknown, known], OfferRequest()) is known
    with pytest.raises(ValueError, match="no compute offers"):
        choose_offer([unknown], OfferRequest())


def test_catalog_admission_depends_on_type_region_and_market() -> None:
    policy = ProviderCapacityPolicy(
        default_region="us-east-1",
        allowed_regions=("us-east-1", "us-west-2"),
        allowed_offers=(ProviderOfferEligibility(region="us-east-1", instance_type="approved"),),
    )
    offer = _offer("approved", 4_000, 8 * 1024)
    assert policy.accepts(offer)
    assert policy.accepts(offer.model_copy(update={"cost_terms": SupplierCostTerms()}))
    assert not policy.accepts(offer.model_copy(update={"region": "us-west-2"}))
    assert not policy.accepts(offer.model_copy(update={"instance_type": "unapproved"}))
    assert not policy.accepts(offer.model_copy(update={"preemptible": True}))
