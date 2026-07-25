from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from pydantic import Field
from shared.compute_policy import ComputeCapacityMode
from shared.contracts import ContractModel
from shared.deployment_records import Resources


@dataclass(frozen=True)
class ComputePlan:
    provider: str
    resources: Resources
    estimated_hourly_cost: float = 0.0


def plan_local_compute(resources: Resources) -> ComputePlan:
    return ComputePlan(provider="local", resources=resources)


class ReservationStatus(StrEnum):
    Pending = "pending"
    Active = "active"
    Terminating = "terminating"
    Failed = "failed"
    Deleted = "deleted"


class ReservationSource(StrEnum):
    Autosolver = "autosolver"
    CliReservation = "cli_reservation"
    Attached = "attached"
    Manual = "manual"

    @property
    def managed(self) -> bool:
        return self not in {ReservationSource.Attached, ReservationSource.Manual}


class SolveActionType(StrEnum):
    Create = "create"
    Keep = "keep"
    Delete = "delete"


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
    runtime: str = "runc"
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


class ReservationPlan(ContractModel):
    offer: ComputeOffer
    name: str
    image: str = "ubuntu-24.04"
    ssh_keys: list[str] = Field(default_factory=list)
    user_data: str | None = None


def plan_compute_reservation(
    offer: ComputeOffer,
    *,
    name: str,
    image: str = "ubuntu-24.04",
) -> ReservationPlan:
    return ReservationPlan(offer=offer, name=name, image=image)


class ComputeReservation(ContractModel):
    id: str
    pool_name: str = ""
    selector: str = ""
    provider: str = ""
    cloud: str = ""
    region: str = ""
    offer_id: str = ""
    instance_type: str = ""
    instance_id: str = ""
    machine_id: str = ""
    gpu: str | None = None
    gpu_count: int = 0
    node_count: int = 0
    cpu_millicores: int = 0
    memory_mb: int = 0
    storage_mb: int = 0
    architecture: str = ""
    runtime: str = ""
    hourly_cost_micros: int = 0
    committed_micros: int = 0
    source: ReservationSource | str = ReservationSource.Attached
    status: ReservationStatus | str = ReservationStatus.Pending
    created_at_seconds: int = 0
    expires_at_seconds: int = 0
    billing_renewal_at_seconds: int = 0

    @property
    def managed(self) -> bool:
        return ReservationSource(str(self.source)).managed

    def active_at(self, now_seconds: int) -> bool:
        if str(self.status) in {
            ReservationStatus.Deleted.value,
            ReservationStatus.Failed.value,
            ReservationStatus.Terminating.value,
        }:
            return False
        return self.expires_at_seconds <= 0 or self.expires_at_seconds > now_seconds


class ComputeDemand(ContractModel):
    pool_name: str
    selector: str = ""
    gpu: list[str] = Field(default_factory=list)
    min_cpu_millicores: int = 0
    min_memory_mb: int = 0
    min_storage_mb: int = 0
    architecture: str = "amd64"
    runtime: str = "runc"
    nodes: int
    offer_id: str = ""
    ttl_seconds: int
    max_spend_micros: int
    providers: list[str] = Field(default_factory=list)
    regions: list[str] = Field(default_factory=list)
    min_reliability: float = 0.0


class ComputeSolveAction(ContractModel):
    action: SolveActionType
    offer: ComputeOffer | None = None
    reservation: ComputeReservation | None = None
    count: int = 0
    cost_micros: int = 0
    reason: str = ""


class ComputeSolvePlan(ContractModel):
    feasible: bool
    reason: str = ""
    actions: list[ComputeSolveAction] = Field(default_factory=list)
    total_capacity: int = 0
    existing_capacity: int = 0
    new_capacity: int = 0
    incremental_cost_micros: int = 0
    committed_cost_micros: int = 0


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


def solve_compute_capacity(
    demand: ComputeDemand,
    offers: list[ComputeOffer],
    reservations: list[ComputeReservation] | None = None,
    *,
    now_seconds: int = 0,
    max_offers: int = 32,
) -> ComputeSolvePlan:
    if demand.nodes <= 0:
        return ComputeSolvePlan(feasible=False, reason="pool reservations require nodes > 0")
    if demand.ttl_seconds <= 0:
        return ComputeSolvePlan(feasible=False, reason="pool reservations require ttl")
    if demand.max_spend_micros <= 0:
        return ComputeSolvePlan(feasible=False, reason="pool reservations require max_spend")
    if demand.min_reliability < 0 or demand.min_reliability > 1:
        return ComputeSolvePlan(feasible=False, reason="min_reliability must be between 0 and 1")

    current_seconds = now_seconds
    existing_capacity, committed_cost, keep, delete = _usable_reservations(
        reservations or [],
        demand,
        current_seconds,
    )
    if existing_capacity >= demand.nodes:
        return ComputeSolvePlan(
            feasible=True,
            actions=[*keep, *delete],
            total_capacity=existing_capacity,
            existing_capacity=existing_capacity,
            committed_cost_micros=committed_cost,
        )

    needed_capacity = demand.nodes - existing_capacity
    candidates = _filter_demand_offers(offers, demand)
    candidates.sort(key=lambda item: (offer_cost_per_node(item), -item.reliability))
    if max_offers > 0:
        candidates = candidates[:max_offers]

    lease_hours = whole_hours(demand.ttl_seconds)
    solution = _solve_bounded(candidates, needed_capacity, lease_hours)
    if solution is None:
        return ComputeSolvePlan(
            feasible=False,
            reason="insufficient compatible capacity",
            actions=[*keep, *delete],
            existing_capacity=existing_capacity,
            committed_cost_micros=committed_cost,
        )

    counts, incremental_cost = solution
    total_commitment = committed_cost + incremental_cost
    if demand.max_spend_micros > 0 and total_commitment > demand.max_spend_micros:
        return ComputeSolvePlan(
            feasible=False,
            reason="max spend would be exceeded",
            actions=[*keep, *delete],
            existing_capacity=existing_capacity,
            committed_cost_micros=committed_cost,
        )

    actions = list(keep)
    new_capacity = 0
    for index, count in enumerate(counts):
        if count <= 0:
            continue
        offer = candidates[index]
        capacity = offer_node_capacity(offer) * count
        new_capacity += capacity
        actions.append(
            ComputeSolveAction(
                action=SolveActionType.Create,
                offer=offer,
                count=count,
                cost_micros=offer.hourly_cost_micros * count * lease_hours,
                reason="satisfy reserved pool demand",
            )
        )
    actions.extend(delete)
    return ComputeSolvePlan(
        feasible=True,
        actions=actions,
        total_capacity=existing_capacity + new_capacity,
        existing_capacity=existing_capacity,
        new_capacity=new_capacity,
        incremental_cost_micros=incremental_cost,
        committed_cost_micros=total_commitment,
    )


def offer_node_capacity(offer: ComputeOffer) -> int:
    if offer.node_count > 0:
        return offer.node_count
    if offer.gpu_count > 0 or offer.cpu_millicores > 0:
        return 1
    return 0


def reservation_node_capacity(reservation: ComputeReservation) -> int:
    if reservation.node_count > 0:
        return reservation.node_count
    if reservation.gpu_count > 0 or reservation.cpu_millicores > 0:
        return 1
    return 0


def offer_cost_per_node(offer: ComputeOffer) -> float:
    capacity = offer_node_capacity(offer)
    if capacity <= 0:
        return float("inf")
    return offer.hourly_cost_micros / capacity


def whole_hours(seconds: int) -> int:
    if seconds <= 0:
        return 0
    return max((seconds + 3599) // 3600, 1)


def _filter_demand_offers(offers: list[ComputeOffer], demand: ComputeDemand) -> list[ComputeOffer]:
    request = OfferRequest(
        providers=demand.providers,
        regions=demand.regions,
        offer_id=demand.offer_id,
        min_cpu_millicores=demand.min_cpu_millicores,
        min_memory_mb=demand.min_memory_mb,
        min_storage_mb=demand.min_storage_mb,
        architecture=demand.architecture,
        runtime=demand.runtime,
        gpus=demand.gpu,
        min_gpu_count=1 if demand.gpu else 0,
        nodes=demand.nodes,
        min_reliability=demand.min_reliability,
    )
    return filter_offers(offers, request)


def _usable_reservations(
    reservations: list[ComputeReservation],
    demand: ComputeDemand,
    now_seconds: int,
) -> tuple[int, int, list[ComputeSolveAction], list[ComputeSolveAction]]:
    total_capacity = 0
    committed_cost = 0
    keep: list[ComputeSolveAction] = []
    delete: list[ComputeSolveAction] = []
    lease_end = now_seconds + demand.ttl_seconds

    for reservation in reservations:
        if not reservation.active_at(now_seconds):
            continue
        if not _reservation_matches_demand(reservation, demand):
            continue
        capacity = reservation_node_capacity(reservation)
        total_capacity += capacity
        cost = (
            0
            if not reservation.managed
            else _existing_reservation_commitment(
                reservation,
                now_seconds,
                lease_end,
            )
        )
        committed_cost += cost
        keep.append(
            ComputeSolveAction(
                action=SolveActionType.Keep,
                reservation=reservation,
                count=1,
                cost_micros=cost,
                reason=(
                    "attached capacity has no incremental provider cost"
                    if not reservation.managed
                    else "existing reservation committed through aggregate deadline"
                ),
            )
        )

    for reservation in reservations:
        if not reservation.managed or not reservation.active_at(now_seconds):
            continue
        if reservation.billing_renewal_at_seconds <= 0:
            continue
        if reservation.billing_renewal_at_seconds > now_seconds:
            continue
        if _reservation_matches_demand(reservation, demand):
            continue
        delete.append(
            ComputeSolveAction(
                action=SolveActionType.Delete,
                reservation=reservation,
                count=1,
                reason="reservation reached renewal boundary and is not needed",
            )
        )

    return total_capacity, committed_cost, keep, delete


def _reservation_matches_demand(reservation: ComputeReservation, demand: ComputeDemand) -> bool:
    if demand.selector and reservation.selector and reservation.selector != demand.selector:
        return False
    if demand.pool_name and reservation.pool_name and reservation.pool_name != demand.pool_name:
        return False
    if demand.gpu and reservation.gpu not in demand.gpu:
        return False
    if reservation.cpu_millicores < demand.min_cpu_millicores:
        return False
    if reservation.memory_mb < demand.min_memory_mb:
        return False
    if reservation.storage_mb < demand.min_storage_mb:
        return False
    if demand.architecture and reservation.architecture not in {"", demand.architecture}:
        return False
    if demand.runtime and reservation.runtime not in {"", demand.runtime}:
        return False
    return not (demand.providers and reservation.provider not in demand.providers)


def _existing_reservation_commitment(
    reservation: ComputeReservation,
    now_seconds: int,
    lease_end_seconds: int,
) -> int:
    if not reservation.managed:
        return 0
    if reservation.created_at_seconds > 0:
        projected = reservation.hourly_cost_micros * whole_hours(
            lease_end_seconds - reservation.created_at_seconds
        )
        return max(reservation.committed_micros, projected)
    if reservation.committed_micros <= 0:
        return 0
    if reservation.expires_at_seconds <= 0 or lease_end_seconds <= reservation.expires_at_seconds:
        return reservation.committed_micros
    extension = reservation.hourly_cost_micros * whole_hours(
        lease_end_seconds - max(reservation.expires_at_seconds, now_seconds)
    )
    return reservation.committed_micros + extension


def _solve_bounded(
    offers: list[ComputeOffer],
    needed_capacity: int,
    lease_hours: int,
) -> tuple[list[int], int] | None:
    if needed_capacity <= 0:
        return ([0 for _ in offers], 0)
    lease = max(lease_hours, 1)
    max_capacity = max((offer_node_capacity(offer) for offer in offers), default=0)
    if max_capacity <= 0:
        return None
    limit = needed_capacity + max_capacity
    unreachable = 2**63 - 1
    costs = [unreachable for _ in range(limit + 1)]
    counts = [[0 for _ in offers] for _ in range(limit + 1)]
    costs[0] = 0

    for index, offer in enumerate(offers):
        capacity = offer_node_capacity(offer)
        if capacity <= 0 or offer.available <= 0:
            continue
        unit_cost = offer.hourly_cost_micros * lease
        for _ in range(offer.available):
            next_costs = list(costs)
            next_counts = [list(item) for item in counts]
            for used_capacity in range(limit + 1):
                if costs[used_capacity] == unreachable:
                    continue
                next_capacity = min(used_capacity + capacity, limit)
                next_cost = costs[used_capacity] + unit_cost
                if next_cost < next_costs[next_capacity]:
                    next_costs[next_capacity] = next_cost
                    selected = list(counts[used_capacity])
                    selected[index] += 1
                    next_counts[next_capacity] = selected
            costs = next_costs
            counts = next_counts

    best_capacity = -1
    best_cost = unreachable
    for capacity in range(needed_capacity, limit + 1):
        if costs[capacity] < best_cost:
            best_cost = costs[capacity]
            best_capacity = capacity
    if best_capacity < 0:
        return None
    return (counts[best_capacity], best_cost)
