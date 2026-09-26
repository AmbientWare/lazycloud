"""Costed node combinations for a bounded batch of compatible container requests."""

from collections.abc import Sequence
from dataclasses import dataclass

from compute.fleet_resources import Capacity, ReserveMarket, ReserveOffer


@dataclass(frozen=True, slots=True)
class CapacityPurchase:
    offer_key: str
    count: int


@dataclass(frozen=True, slots=True)
class CapacityNode:
    offer_key: str
    request_indices: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class CapacityAcquisitionPlan:
    purchases: tuple[CapacityPurchase, ...]
    remaining: tuple[Capacity, ...]
    nodes: tuple[CapacityNode, ...]


@dataclass(frozen=True, slots=True)
class _Packing:
    cost: int
    remaining: tuple[int, ...]
    cpu: int
    offers: tuple[int, ...]
    allocations: tuple[tuple[int, ...], ...]


def plan_request_capacity(
    offers: Sequence[ReserveOffer],
    requests: Sequence[Capacity],
    *,
    machine_limit: int,
    running_cpu_millicores: int,
) -> CapacityAcquisitionPlan:
    """Select a bounded-cost packing; every request must fit a single selected node.

    Callers filter offers and requests to the same placement and runtime constraints.
    Search keeps at most 64 incomplete packings after each added node. It returns
    uncovered requests explicitly when prices, limits or available shapes prevent
    a complete plan. Complete plans minimize cost among the packings explored.
    """
    if any(not request.covers(Capacity()) for request in requests):
        raise ValueError("requested capacity cannot be negative")
    if any(offer.hourly_cost_micros < 0 for offer in offers):
        raise ValueError("offer cost cannot be negative")
    by_shape: dict[tuple[ReserveMarket, Capacity, int], ReserveOffer] = {}
    for offer in offers:
        if not any(offer.machine.covers(request) for request in requests):
            continue
        key = (offer.market, offer.machine, offer.nominal_cpu_millicores)
        previous = by_shape.get(key)
        if previous is None or offer.hourly_cost_micros < previous.hourly_cost_micros:
            by_shape[key] = offer
    candidates = tuple(by_shape.values())
    order = tuple(
        sorted(
            range(len(requests)),
            key=lambda index: (
                requests[index].gpu_count,
                requests[index].memory_mib,
                requests[index].cpu_millicores,
            ),
            reverse=True,
        )
    )
    states = [_Packing(0, order, 0, (), ())]
    partial = states[0]
    best: _Packing | None = None
    for _ in range(max(0, min(machine_limit, len(requests)))):
        expanded: dict[tuple[tuple[int, ...], int], _Packing] = {}
        for state in states:
            for offer_index, offer in enumerate(candidates):
                cpu = state.cpu + (0 if offer.market.gpu_type else offer.nominal_cpu_millicores)
                cost = state.cost + offer.hourly_cost_micros
                if cpu > running_cpu_millicores or (best is not None and cost >= best.cost):
                    continue
                free = offer.machine
                remaining: list[int] = []
                placed: list[int] = []
                for index in state.remaining:
                    if free.covers(requests[index]):
                        free -= requests[index]
                        placed.append(index)
                    else:
                        remaining.append(index)
                if len(remaining) == len(state.remaining):
                    continue
                next_state = _Packing(
                    cost,
                    tuple(remaining),
                    cpu,
                    (*state.offers, offer_index),
                    (*state.allocations, tuple(placed)),
                )
                if not remaining:
                    best = next_state
                else:
                    key = (next_state.remaining, cpu)
                    if key not in expanded or cost < expanded[key].cost:
                        expanded[key] = next_state
        states = sorted(
            expanded.values(),
            key=lambda state: (
                state.cost / (len(requests) - len(state.remaining)),
                state.cost,
                state.offers,
            ),
        )[:64]
        if not states:
            break
        partial = min((partial, *states), key=lambda state: (len(state.remaining), state.cost))
    selected = best or partial
    counts: dict[str, int] = {}
    for index in selected.offers:
        key = candidates[index].key
        counts[key] = counts.get(key, 0) + 1
    return CapacityAcquisitionPlan(
        purchases=tuple(CapacityPurchase(key, count) for key, count in counts.items()),
        remaining=tuple(requests[index] for index in selected.remaining),
        nodes=tuple(
            CapacityNode(candidates[index].key, allocations)
            for index, allocations in zip(selected.offers, selected.allocations, strict=True)
        ),
    )
