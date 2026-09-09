from __future__ import annotations

from enum import StrEnum

from pydantic import Field
from shared.compute_policy import ComputeCapacityMode, ComputeUnitRecord
from shared.container_requests import OciRuntimeName, capacity_with_overhead
from shared.contracts import ContractModel
from shared.gpu import gpu_preference_accepts, gpu_preference_rank
from shared.supplier_costs import SupplierCostTerms, SupplierCpuUnit


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
    runtime: str = OciRuntimeName.Runsc.value
    gpu: str | None = None
    gpu_count: int = 0
    node_count: int = 0
    cost_terms: SupplierCostTerms = Field(default_factory=SupplierCostTerms)
    supplier_cpu_unit: SupplierCpuUnit = SupplierCpuUnit.Unknown
    supplier_cpu_count: int | None = Field(default=None, ge=0)
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

    @property
    def hourly_cost_micros(self) -> int | None:
        return self.cost_terms.known_hourly_cost_micros

    @property
    def billing_minimum_seconds(self) -> int | None:
        return self.cost_terms.billing_minimum_seconds

    @property
    def billing_quantum_seconds(self) -> int | None:
        return self.cost_terms.billing_quantum_seconds


# What a pooled cloud node is, independent of whose cloud it is. A provider that
# restated these would be free to drift from the others, and the drift would show
# up as a workload that fits on one cloud and not another for reasons nobody
# chose.
DEFAULT_POOLED_NODE_STORAGE_MB = 200 * 1024
DEFAULT_POOLED_NODE_ARCHITECTURE = "amd64"
DEFAULT_POOLED_NODE_RUNTIME = OciRuntimeName.Runsc.value
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
    cost_terms: SupplierCostTerms,
    capability_key: str,
    supplier_cpu_unit: SupplierCpuUnit = SupplierCpuUnit.Unknown,
    supplier_cpu_count: int | None = None,
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
        cost_terms=cost_terms,
        supplier_cpu_unit=supplier_cpu_unit,
        supplier_cpu_count=supplier_cpu_count,
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
    gpu: list[str] = Field(default_factory=list)
    """Models this request accepts, best first; empty asks for no GPU."""

    min_gpu_count: int = 0
    nodes: int = 0
    min_reliability: float = 0.0
    max_hourly_cost_micros: int = 0


def recorded_unit_offer(
    unit: ComputeUnitRecord, *, cloud: str, instance_type: str, architecture: str = "amd64"
) -> ComputeOffer:
    """Describe owned capacity without depending on the supplier's sale catalog."""
    if len(unit.worker_runtimes) != 1:
        raise ValueError("provider unit has incomplete recorded offer data")
    return pooled_cloud_offer(
        offer_id=unit.offer_id,
        provider=unit.provider_ref,
        cloud=cloud,
        instance_type=instance_type,
        region=unit.region,
        cpu_millicores=unit.worker_cpu_millicores,
        memory_mb=unit.worker_memory_mib,
        storage_mb=unit.offer_storage_mib if unit.offer_storage_mib is not None else 0,
        gpu=unit.worker_gpu_type or None,
        gpu_count=unit.worker_gpu_count,
        cost_terms=(
            unit.offer_cost_terms if unit.offer_cost_terms is not None else SupplierCostTerms()
        ),
        supplier_cpu_unit=unit.supplier_cpu_unit,
        supplier_cpu_count=unit.supplier_cpu_count,
        capability_key=unit.capability_key,
        architecture=architecture,
        runtime=unit.worker_runtimes[0],
    )


def filter_offers(offers: list[ComputeOffer], request: OfferRequest) -> list[ComputeOffer]:
    required_cpu = capacity_with_overhead(request.min_cpu_millicores)
    required_memory = capacity_with_overhead(request.min_memory_mb)
    selected: list[ComputeOffer] = []
    for offer in offers:
        if request.offer_id and offer.id != request.offer_id:
            continue
        if request.providers and offer.provider not in request.providers:
            continue
        if request.regions and offer.region not in request.regions:
            continue
        if offer.cpu_millicores < required_cpu:
            continue
        if offer.memory_mb < required_memory:
            continue
        if offer.storage_mb < request.min_storage_mb:
            continue
        if request.architecture and offer.architecture != request.architecture:
            continue
        if request.runtime and offer.runtime != request.runtime:
            continue
        # Through the shared rule rather than a string compare, so an offer for a
        # card the request would accept is not passed over for spelling it the
        # way the provider does, and `any` means here what it means everywhere.
        if request.gpu and not gpu_preference_accepts(request.gpu, offer.gpu or ""):
            continue
        if offer.gpu_count < request.min_gpu_count:
            continue
        if request.nodes > 0 and not request.gpu and offer.gpu_count > 0:
            continue
        if (
            request.min_reliability > 0
            and offer.reliability > 0
            and offer.reliability < request.min_reliability
        ):
            continue
        if request.max_hourly_cost_micros > 0 and (
            offer.cost_terms.complete_hourly_cost_micros is None
            or offer.cost_terms.complete_hourly_cost_micros > request.max_hourly_cost_micros
        ):
            continue
        if offer.available <= 0:
            continue
        selected.append(offer)
    return selected


def choose_offer(offers: list[ComputeOffer], request: OfferRequest) -> ComputeOffer:
    candidates = [
        offer
        for offer in filter_offers(offers, request)
        if offer.cost_terms.complete_hourly_cost_micros is not None
    ]
    if not candidates:
        msg = "no compute offers match request"
        raise ValueError(msg)

    return min(candidates, key=lambda item: offer_selection_key(item, request))


def offer_selection_key(
    offer: ComputeOffer, request: OfferRequest
) -> tuple[int, float, float, str, str]:
    rank = gpu_preference_rank(request.gpu, offer.gpu or "")
    return (
        len(request.gpu) if rank is None else rank,
        offer_cost_per_node(offer),
        -offer.reliability,
        offer.provider,
        offer.id,
    )


def offer_node_capacity(offer: ComputeOffer) -> int:
    if offer.node_count > 0:
        return offer.node_count
    if offer.gpu_count > 0 or offer.cpu_millicores > 0:
        return 1
    return 0


def offer_cost_per_node(offer: ComputeOffer) -> float:
    capacity = offer_node_capacity(offer)
    cost = offer.cost_terms.complete_hourly_cost_micros
    if capacity <= 0 or cost is None:
        return float("inf")
    return cost / capacity


__all__ = [
    "ComputeOffer",
    "OfferRequest",
    "ReservationStatus",
    "choose_offer",
    "filter_offers",
    "offer_selection_key",
]
