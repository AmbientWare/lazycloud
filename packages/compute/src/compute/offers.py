from __future__ import annotations

from enum import StrEnum

from pydantic import Field
from shared.compute_policy import ComputeCapacityMode
from shared.contracts import ContractModel


class ReservationStatus(StrEnum):
    Pending = "pending"
    Active = "active"
    Terminating = "terminating"
    Failed = "failed"
    Deleted = "deleted"


class ComputeOffer(ContractModel):
    id: str
    provider: str
    cloud: str = ""
    instance_type: str
    region: str
    cpu_millicores: int = 0
    memory_mb: int = 0
    storage_mb: int = 0
    architecture: str = "amd64"
    runtime: str = "runsc"
    gpu: str | None = None
    gpu_count: int = 0
    node_count: int = 0
    hourly_cost_micros: int = 0
    reliability: float = 0.0
    available: int = 1
    capacity_mode: ComputeCapacityMode = ComputeCapacityMode.Direct
    capability_key: str = ""
    supports_scale_to_zero: bool = False
    display_name: str = ""
    category: str = ""
    region_display_name: str = ""
    latitude: float = 0.0
    longitude: float = 0.0
    labels: dict[str, str] = Field(default_factory=dict)


# What a pooled cloud node is, independent of whose cloud it is. A provider that
# restated these would be free to drift from the others, and the drift would show
# up as a workload that fits on one cloud and not another for reasons nobody
# chose.
DEFAULT_POOLED_NODE_STORAGE_MB = 200 * 1024
DEFAULT_POOLED_NODE_ARCHITECTURE = "amd64"
DEFAULT_POOLED_NODE_RUNTIME = "runsc"
DEFAULT_POOLED_NODE_AVAILABILITY = 100


def pooled_cloud_offer(
    *,
    offer_id: str,
    provider: str,
    cloud: str,
    instance_type: str,
    region: str,
    cpu_millicores: int,
    memory_mb: int,
    hourly_cost_micros: int,
    capability_key: str,
    gpu: str | None = None,
    gpu_count: int = 0,
    storage_mb: int = DEFAULT_POOLED_NODE_STORAGE_MB,
    architecture: str = DEFAULT_POOLED_NODE_ARCHITECTURE,
    runtime: str = DEFAULT_POOLED_NODE_RUNTIME,
) -> ComputeOffer:
    """One node of pooled capacity, described the same way whoever rents it.

    A provider supplies only what is genuinely its own — the instance type, the
    region, the price, the hardware. Everything else is a platform decision and
    lives here, so adding a second cloud cannot quietly disagree with the first
    about what a node is or which runtime it runs.
    """
    return ComputeOffer(
        id=offer_id,
        provider=provider,
        cloud=cloud,
        instance_type=instance_type,
        region=region,
        cpu_millicores=cpu_millicores,
        memory_mb=memory_mb,
        storage_mb=storage_mb,
        architecture=architecture,
        runtime=runtime,
        gpu=gpu,
        gpu_count=gpu_count,
        node_count=1,
        hourly_cost_micros=hourly_cost_micros,
        reliability=1.0,
        available=DEFAULT_POOLED_NODE_AVAILABILITY,
        capacity_mode=ComputeCapacityMode.Pooled,
        capability_key=capability_key,
        supports_scale_to_zero=True,
    )


class OfferRequest(ContractModel):
    providers: list[str] = Field(default_factory=list)
    regions: list[str] = Field(default_factory=list)
    offer_id: str = ""
    min_cpu_millicores: int = 0
    min_memory_mb: int = 0
    min_storage_mb: int = 0
    architecture: str = ""
    runtime: str = ""
    gpu: str | None = None
    gpus: list[str] = Field(default_factory=list)
    min_gpu_count: int = 0
    nodes: int = 0
    min_reliability: float = 0.0
    max_hourly_cost_micros: int = 0


def filter_offers(offers: list[ComputeOffer], request: OfferRequest) -> list[ComputeOffer]:
    selected: list[ComputeOffer] = []
    for offer in offers:
        if request.offer_id and offer.id != request.offer_id:
            continue
        if request.providers and offer.provider not in request.providers:
            continue
        if request.regions and offer.region not in request.regions:
            continue
        if offer.cpu_millicores < request.min_cpu_millicores:
            continue
        if offer.memory_mb < request.min_memory_mb:
            continue
        if offer.storage_mb < request.min_storage_mb:
            continue
        if request.architecture and offer.architecture != request.architecture:
            continue
        if request.runtime and offer.runtime != request.runtime:
            continue
        requested_gpus = request.gpus or ([request.gpu] if request.gpu is not None else [])
        if requested_gpus and offer.gpu not in requested_gpus:
            continue
        if offer.gpu_count < request.min_gpu_count:
            continue
        if request.nodes > 0 and not requested_gpus and offer.gpu_count > 0:
            continue
        if (
            request.min_reliability > 0
            and offer.reliability > 0
            and offer.reliability < request.min_reliability
        ):
            continue
        if (
            request.max_hourly_cost_micros > 0
            and offer.hourly_cost_micros > request.max_hourly_cost_micros
        ):
            continue
        if offer.available <= 0:
            continue
        selected.append(offer)
    return selected


def choose_offer(offers: list[ComputeOffer], request: OfferRequest) -> ComputeOffer:
    candidates = filter_offers(offers, request)
    if not candidates:
        msg = "no compute offers match request"
        raise ValueError(msg)
    return min(candidates, key=lambda item: (offer_cost_per_node(item), -item.reliability))


def offer_node_capacity(offer: ComputeOffer) -> int:
    if offer.node_count > 0:
        return offer.node_count
    if offer.gpu_count > 0 or offer.cpu_millicores > 0:
        return 1
    return 0


def offer_cost_per_node(offer: ComputeOffer) -> float:
    capacity = offer_node_capacity(offer)
    if capacity <= 0:
        return float("inf")
    return offer.hourly_cost_micros / capacity


__all__ = [
    "ComputeOffer",
    "OfferRequest",
    "ReservationStatus",
    "choose_offer",
    "filter_offers",
]
