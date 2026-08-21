from __future__ import annotations

from compute.offers import ComputeOffer, OfferRequest, filter_offers, pooled_cloud_offer


def _offer(instance_type: str, cpu_millicores: int, memory_mb: int) -> ComputeOffer:
    return pooled_cloud_offer(
        offer_id=f"us-east-1:{instance_type}",
        provider="aws",
        cloud="aws",
        instance_type=instance_type,
        region="us-east-1",
        cpu_millicores=cpu_millicores,
        memory_mb=memory_mb,
        hourly_cost_micros=100_000,
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
