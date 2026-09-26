"""How much spare capacity the platform fleet keeps, and the pure plan that keeps it.

Headroom is measured in CPU, memory and GPU cards per market rather than in
machines, because a request needs room of a size and not a machine count. A
market is the purchase market a workload accepted, Spot or On-Demand, and for
GPUs the card as well. Every figure here is schedulable capacity: what a node
gives containers after `schedulable_capacity`, less what live containers
reserve.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from enum import StrEnum

from pydantic import Field
from shared.contracts import ContractModel
from shared.gpu import GpuType, normalize_gpu_type

from compute.capacity_acquisition import plan_request_capacity
from compute.fleet_resources import (
    Capacity,
    ReserveDemand,
    ReserveMarket,
    ReserveOffer,
    ReservePlacement,
)

RESERVE_PLAN_INTERVAL_SECONDS = 60
RESERVE_EARLY_PLAN_INTERVAL_SECONDS = 20


def _total(items: Iterable[Capacity]) -> Capacity:
    result = Capacity()
    for item in items:
        result = result + item
    return result


class HeadroomTarget(ContractModel):
    """Minimum spare capacity and the share of current allocations kept free."""

    floor: Capacity = Capacity()
    load_percent: int = Field(default=0, ge=0, le=100)

    def target(self, load: Capacity) -> Capacity:
        return self.floor.upper(load.percent(self.load_percent))


class MarketReserve(ContractModel):
    """Running headroom a market keeps warm, and headroom it keeps as stopped machines."""

    warm: HeadroomTarget = HeadroomTarget()
    stopped: HeadroomTarget = HeadroomTarget()


_GIB = 1024

_SPOT_RESERVE = MarketReserve(
    warm=HeadroomTarget(
        floor=Capacity(2_000, 4 * _GIB),
        load_percent=25,
    ),
    stopped=HeadroomTarget(
        floor=Capacity(6_000, 12 * _GIB),
        load_percent=50,
    ),
)
_ON_DEMAND_RESERVE = MarketReserve(
    warm=HeadroomTarget(
        floor=Capacity(2_000, 4 * _GIB),
        load_percent=25,
    ),
    stopped=HeadroomTarget(
        floor=Capacity(6_000, 12 * _GIB),
        load_percent=50,
    ),
)
_ONE_CARD_RESERVE = MarketReserve(
    warm=HeadroomTarget(load_percent=25),
    stopped=HeadroomTarget(load_percent=50),
)


class FleetCapacityPolicy(ContractModel):
    minimum_purchase_margin_percent: int = Field(default=30, ge=0, lt=100)
    resume_seconds: int = Field(default=30, gt=0)
    stopped_boot_seconds: int = Field(default=120, gt=0)
    provision_seconds: int = Field(default=300, gt=0)
    cost_horizon_seconds: int = Field(default=3600, gt=0)
    max_growth_actions: int = Field(default=16, gt=0)
    """Provider actions admitted per plan; outstanding shortfalls remain eligible next pass."""
    spot: MarketReserve = _SPOT_RESERVE
    on_demand: MarketReserve = _ON_DEMAND_RESERVE
    gpu: dict[str, MarketReserve] = Field(
        default_factory=lambda: {
            GpuType.T4.value: _ONE_CARD_RESERVE,
            GpuType.A10G.value: _ONE_CARD_RESERVE,
            GpuType.L4.value: _ONE_CARD_RESERVE,
        }
    )
    """On-Demand reserves per card. A card left out keeps no GPU headroom."""

    pressure_seconds: int = Field(default=5, ge=1)
    """How long running headroom stays short before the planner runs early."""

    consolidation_percent: int = Field(default=30, ge=0, le=100)
    consolidation_seconds: int = Field(default=600, ge=0)
    consolidation_cooldown_seconds: int = Field(default=900, ge=0)
    consolidation_deadline_seconds: int = Field(default=3600, gt=0)

    @property
    def warm_forecast_seconds(self) -> int:
        return self.resume_seconds + RESERVE_PLAN_INTERVAL_SECONDS

    @property
    def total_forecast_seconds(self) -> int:
        return self.provision_seconds + RESERVE_PLAN_INTERVAL_SECONDS

    def reserve(self, market: ReserveMarket) -> MarketReserve:
        if market.gpu_type:
            if market.preemptible:
                return MarketReserve()
            return self.gpu.get(normalize_gpu_type(market.gpu_type), MarketReserve())
        return self.spot if market.preemptible else self.on_demand

    def markets(self) -> tuple[ReserveMarket, ...]:
        return (
            ReserveMarket(preemptible=True),
            ReserveMarket(preemptible=False),
            *(ReserveMarket(preemptible=False, gpu_type=card) for card in sorted(self.gpu)),
        )


class ReserveMachineState(StrEnum):
    Serving = "serving"
    Starting = "starting"
    """Launched, resuming or joining: capacity that will serve without another purchase."""

    Draining = "draining"
    """Cordoned or interrupted, so it gives the market no headroom."""

    Preparing = "preparing"
    Stopped = "stopped"
    HibernationRequested = "hibernation_requested"
    Hibernated = "hibernated"

    @property
    def stopped(self) -> bool:
        return self in {self.Stopped, self.HibernationRequested, self.Hibernated}

    @property
    def reserve(self) -> bool:
        return self is self.Preparing or self.stopped


@dataclass(frozen=True, slots=True)
class ReserveUnit:
    unit_id: str
    market: ReserveMarket
    machine: Capacity
    """Schedulable capacity of one of this unit's machines."""

    nominal_cpu_millicores: int
    desired: int
    stopped: int
    growable: bool
    """Whether a resume or purchase may go here: healthy, not cooling, and purchasable."""

    enabled: bool = True
    """Whether its provider may purchase at all. A disabled unit holds no reserves
    and its idle machines drain; its capacity does not count as headroom."""
    hourly_cost_micros: int | None = None
    stopped_hourly_cost_micros: int | None = None
    placement: ReservePlacement = field(default_factory=ReservePlacement)
    supports_hibernation: bool = False


@dataclass(frozen=True, slots=True)
class ReserveMachine:
    key: str
    """The machine id, or the provider instance for one that has not enrolled."""

    unit_id: str
    state: ReserveMachineState
    load: Capacity = field(default_factory=Capacity)
    containers: int = 0
    pinned: int = 0
    """Live containers that did not accept interruption, so they cannot be moved."""

    protected: bool = False
    """A recovery source, a recovery replacement, or a planned replacement's pair."""

    billing_settled: bool = True
    ready: bool = True
    """Fresh compatible intake, or verified compatible preparation for a stopped host."""


@dataclass(frozen=True, slots=True)
class FleetReserveSnapshot:
    units: tuple[ReserveUnit, ...]
    machines: tuple[ReserveMachine, ...]
    committed_cpu_machines: int
    committed_gpu_machines: int
    running_cpu_millicores: int
    """Nominal CPU of the running CPU fleet, including preparation and cleanup."""
    offers: tuple[ReserveOffer, ...] = ()


class GrowthKind(StrEnum):
    Resume = "resume"
    Buy = "buy"
    Prepare = "prepare"


@dataclass(frozen=True, slots=True)
class ReserveGrowth:
    kind: GrowthKind
    unit_id: str = ""
    offer_key: str = ""
    count: int = 1


@dataclass(frozen=True, slots=True)
class MarketReservePlan:
    market: ReserveMarket
    load: Capacity
    quiet: bool
    warm_target: Capacity
    warm_free: Capacity
    stopped_target: Capacity
    stopped_capacity: Capacity
    stopped_ready: Capacity = field(default_factory=Capacity)
    stopped_pending: Capacity = field(default_factory=Capacity)
    hibernated_capacity: Capacity = field(default_factory=Capacity)
    hibernation_requested_capacity: Capacity = field(default_factory=Capacity)
    hibernated_target: Capacity = field(default_factory=Capacity)
    hibernated_shortfall: Capacity = field(default_factory=Capacity)
    growth: tuple[ReserveGrowth, ...] = ()
    warm_pending: Capacity = field(default_factory=Capacity)
    shortfall: Capacity = field(default_factory=Capacity)
    stopped_shortfall: Capacity = field(default_factory=Capacity)
    unmet_shapes: tuple[Capacity, ...] = ()
    unmet_stopped_shapes: tuple[Capacity, ...] = ()
    reason: str = ""
    retained: Mapping[str, int] = field(default_factory=dict[str, int])
    """Serving machines each unit keeps from the idle drain."""

    stopped: Mapping[str, int] = field(default_factory=dict[str, int])
    """Stopped reserves each unit holds after retiring what the market no longer needs."""

    consolidation_candidate: str = ""
    """The machine consolidation would empty, placed onto last while it is watched."""

    consolidate: str = ""
    """The candidate, once it has been lightly used for long enough to move its work."""
    consolidation_destinations: tuple[str, ...] = ()
    rightsize_source: str = ""
    rightsize_offer_key: str = ""
    unmet_placements: tuple[ReserveDemand, ...] = ()


@dataclass(frozen=True, slots=True)
class FleetReservePlan:
    markets: tuple[MarketReservePlan, ...]
    lightly_used_since: Mapping[str, datetime]

    def market(self, market: ReserveMarket) -> MarketReservePlan | None:
        return next((plan for plan in self.markets if plan.market == market), None)


@dataclass(frozen=True, slots=True)
class ReserveConditions:
    """What the planner reads besides the fleet: time and what other passes are doing."""

    now: datetime
    lightly_used_since: Mapping[str, datetime] = field(default_factory=dict[str, datetime])
    demand: frozenset[ReserveMarket] = frozenset()
    """Markets with work waiting for capacity, which keeps reserve growth out of its way."""

    recovering: frozenset[ReserveMarket] = frozenset()
    consolidating: frozenset[ReserveMarket] = frozenset()
    """Markets consolidating a machine now or cooling down after one."""
    forecast_warm: Mapping[ReserveMarket, Capacity] = field(
        default_factory=dict[ReserveMarket, Capacity]
    )
    forecast_total: Mapping[ReserveMarket, Capacity] = field(
        default_factory=dict[ReserveMarket, Capacity]
    )
    request_shapes: Mapping[ReserveMarket, tuple[Capacity, ...]] = field(
        default_factory=dict[ReserveMarket, tuple[Capacity, ...]]
    )
    placement_demands: Mapping[ReserveMarket, tuple[ReserveDemand, ...]] = field(
        default_factory=dict[ReserveMarket, tuple[ReserveDemand, ...]]
    )
    hibernated_target: Mapping[ReserveMarket, Capacity] = field(
        default_factory=dict[ReserveMarket, Capacity]
    )


def plan_market_reserve(
    policy: FleetCapacityPolicy,
    snapshot: FleetReserveSnapshot,
    conditions: ReserveConditions,
) -> FleetReservePlan:
    """Decide growth, retention, stopped reserves and consolidation for every market.

    Ready resources and committed resources are distinct. Pending launches prevent
    duplicate purchases but cannot justify retiring a serving machine.
    """
    units = {unit.unit_id: unit for unit in snapshot.units}
    markets = sorted(set(policy.markets()) | {unit.market for unit in snapshot.units})
    lightly_used: dict[str, datetime] = {}
    plans: list[MarketReservePlan] = []
    for market in markets:
        market_units = [unit for unit in snapshot.units if unit.market == market]
        machines = [machine for machine in snapshot.machines if machine.unit_id in units]
        machines = [machine for machine in machines if units[machine.unit_id].market == market]
        plan = _plan_market(
            policy,
            market,
            market_units,
            machines,
            units,
            conditions=conditions,
            lightly_used=lightly_used,
            offers=[offer for offer in snapshot.offers if offer.market == market],
        )
        plans.append(plan)
    return FleetReservePlan(markets=tuple(plans), lightly_used_since=lightly_used)


def _plan_market(
    policy: FleetCapacityPolicy,
    market: ReserveMarket,
    market_units: list[ReserveUnit],
    machines: list[ReserveMachine],
    units: Mapping[str, ReserveUnit],
    *,
    conditions: ReserveConditions,
    lightly_used: dict[str, datetime],
    offers: list[ReserveOffer],
) -> MarketReservePlan:
    units = {unit.unit_id: unit for unit in market_units}
    reserve = policy.reserve(market)
    warm = [
        machine
        for machine in machines
        if machine.state is ReserveMachineState.Serving
        and machine.ready
        and units[machine.unit_id].enabled
    ]
    working = [machine for machine in machines if not machine.state.reserve]
    load = _total(machine.load for machine in working)
    launching = _total(
        unit.machine
        * max(
            unit.desired
            - sum(
                machine.unit_id == unit.unit_id and not machine.state.reserve
                for machine in machines
            ),
            0,
        )
        for unit in market_units
        if unit.enabled
    )
    warm_free = _total(
        (units[machine.unit_id].machine - machine.load).clamped() for machine in warm
    )
    warm_pending = launching + _total(
        units[machine.unit_id].machine
        for machine in machines
        if machine.state is ReserveMachineState.Starting and units[machine.unit_id].enabled
    )
    warm_target = reserve.warm.target(load).upper(conditions.forecast_warm.get(market, Capacity()))
    total_target = (reserve.stopped.target(load) + warm_target).upper(
        conditions.forecast_total.get(market, Capacity())
    )
    stopped_target = (total_target - warm_target).clamped()
    if market.preemptible and not market.gpu_type:
        # An interrupted Spot machine's work must fit the reserves that replace it.
        for machine in working:
            stopped_target = stopped_target.upper(machine.load)
    hibernation_shapes = [
        unit.machine for unit in market_units if unit.enabled and unit.supports_hibernation
    ]
    hibernation_shapes.extend(
        offer.machine for offer in offers if offer.supports_reserve and offer.supports_hibernation
    )
    hibernation_supported = (
        not market.gpu_type
        and bool(hibernation_shapes)
        and all(
            any(capacity.covers(shape) for capacity in hibernation_shapes)
            for shape in conditions.request_shapes.get(market, ())
        )
    )
    hibernated_target = conditions.hibernated_target.get(
        market, stopped_target if hibernation_supported else Capacity()
    )
    stopped_target = stopped_target.upper(hibernated_target)
    quiet = reserve.warm.floor.covers(load)
    deficit = (warm_target - warm_free).clamped()
    may_grow = market not in conditions.demand and market not in conditions.recovering

    growth: list[ReserveGrowth] = []
    pending_deficit = (deficit - warm_pending).clamped()
    placement_demands = conditions.placement_demands.get(market, ())
    _, placement_hosts = _uncovered_placements(placement_demands, machines, units, ())
    shapes = tuple(
        shape
        for shape in conditions.request_shapes.get(market, ())
        if not any(
            (units[machine.unit_id].machine - machine.load).covers(shape) for machine in warm
        )
        and not any(
            units[machine.unit_id].machine.covers(shape)
            for machine in machines
            if machine.state is ReserveMachineState.Starting
        )
        and not any(
            unit.enabled and _pending_slots(unit, machines) and unit.machine.covers(shape)
            for unit in market_units
        )
    )

    candidate, consolidate, destinations = _consolidation(
        policy,
        market,
        [
            replace(machine, protected=True) if machine.key in placement_hosts else machine
            for machine in warm
        ],
        units,
        warm_free=warm_free,
        warm_target=warm_target,
        conditions=conditions,
        lightly_used=lightly_used,
        blocked=not deficit.empty or market in conditions.consolidating,
    )
    moving = next((machine for machine in warm if machine.key == consolidate), None)
    retained = _retention(
        market_units,
        [
            replace(machine, protected=True)
            if machine.key in destinations
            or machine.key in placement_hosts
            or not machine.billing_settled
            or conditions.now - lightly_used.get(machine.key, conditions.now)
            < timedelta(seconds=policy.consolidation_seconds)
            else machine
            for machine in warm
        ],
        units,
        surplus=warm_free - warm_target - (units[moving.unit_id].machine if moving else Capacity()),
        release_largest_first=quiet,
        hold_all=not deficit.empty,
        shapes=conditions.request_shapes.get(market, ()),
    )

    # A unit that cannot grow keeps the reserves it holds and gives up any it
    # was still waiting to launch, so the shortfall goes to another unit.
    stopped = {
        unit.unit_id: 0
        if not unit.enabled
        else unit.stopped
        if unit.growable
        else min(
            unit.stopped,
            sum(machine.unit_id == unit.unit_id and machine.state.reserve for machine in machines),
        )
        for unit in market_units
    }
    stopped_capacity = _total(unit.machine * stopped[unit.unit_id] for unit in market_units)
    if may_grow:
        for unit in sorted(
            market_units,
            key=lambda item: (
                not any(
                    demand.placement.accepts(item.placement)
                    and item.machine.covers(demand.capacity)
                    for demand in placement_demands
                ),
                item.hourly_cost_micros is None,
                item.hourly_cost_micros or 0,
                item.unit_id,
            ),
        ):
            available = min(
                stopped[unit.unit_id],
                sum(
                    machine.unit_id == unit.unit_id
                    and machine.state.stopped
                    and machine.ready
                    and not machine.protected
                    for machine in machines
                ),
            )
            while (
                available
                and unit.growable
                and (not pending_deficit.empty or shapes or placement_demands)
                and sum(action.count for action in growth) < policy.max_growth_actions
            ):
                resumed = tuple(
                    (units[action.unit_id].placement, units[action.unit_id].machine)
                    for action in growth
                    if action.kind is GrowthKind.Resume
                    for _ in range(action.count)
                )
                uncovered, _ = _uncovered_placements(placement_demands, machines, units, resumed)
                if uncovered and not any(
                    demand.placement.accepts(unit.placement)
                    and unit.machine.covers(demand.capacity)
                    for demand in uncovered
                ):
                    break
                if (
                    not uncovered
                    and pending_deficit.empty
                    and not any(unit.machine.covers(shape) for shape in shapes)
                ):
                    break
                growth.append(ReserveGrowth(GrowthKind.Resume, unit_id=unit.unit_id))
                pending_deficit = (pending_deficit - unit.machine).clamped()
                shapes = tuple(shape for shape in shapes if not unit.machine.covers(shape))
                available -= 1
                stopped[unit.unit_id] -= 1
                stopped_capacity = (stopped_capacity - unit.machine).clamped()
        offered = {offer.key: offer for offer in offers}
        planned = tuple(
            (offered[action.offer_key].placement, offered[action.offer_key].machine)
            for action in growth
            if action.kind is GrowthKind.Buy
            for _ in range(action.count)
        ) + tuple(
            (units[action.unit_id].placement, units[action.unit_id].machine)
            for action in growth
            if action.kind is GrowthKind.Resume
            for _ in range(action.count)
        )
        unmet_placements, _ = _uncovered_placements(placement_demands, machines, units, planned)
        for demand in unmet_placements:
            current_unmet, _ = _uncovered_placements(placement_demands, machines, units, planned)
            remaining = next(
                (
                    item.count
                    for item in current_unmet
                    if item.capacity == demand.capacity and item.placement == demand.placement
                ),
                0,
            )
            if not remaining:
                continue
            available_actions = policy.max_growth_actions - sum(action.count for action in growth)
            if available_actions <= 0:
                break
            compatible = [offer for offer in offers if demand.placement.accepts(offer.placement)]
            purchases = plan_request_capacity(
                compatible, [demand.capacity] * min(remaining, available_actions)
            )
            for purchase in purchases.purchases:
                growth.append(
                    ReserveGrowth(
                        GrowthKind.Buy, offer_key=purchase.offer_key, count=purchase.count
                    )
                )
                pending_deficit = (
                    pending_deficit - offered[purchase.offer_key].machine * purchase.count
                ).clamped()
                planned += (
                    (offered[purchase.offer_key].placement, offered[purchase.offer_key].machine),
                ) * purchase.count
                shapes = tuple(
                    shape
                    for shape in shapes
                    if not offered[purchase.offer_key].machine.covers(shape)
                )
        purchases, pending_deficit, shapes = _purchase_growth(
            policy,
            offers,
            pending_deficit,
            shapes,
            reserve=False,
            action_limit=policy.max_growth_actions - sum(action.count for action in growth),
        )
        growth.extend(purchases)
        planned = tuple(
            (offered[action.offer_key].placement, offered[action.offer_key].machine)
            for action in growth
            if action.kind is GrowthKind.Buy
            for _ in range(action.count)
        ) + tuple(
            (units[action.unit_id].placement, units[action.unit_id].machine)
            for action in growth
            if action.kind is GrowthKind.Resume
            for _ in range(action.count)
        )
    else:
        planned = ()
    unmet_placements, _ = _uncovered_placements(placement_demands, machines, units, planned)
    hibernated_capacity = _total(
        unit.machine
        * max(
            0,
            min(
                stopped[unit.unit_id],
                sum(
                    machine.unit_id == unit.unit_id
                    and machine.state is ReserveMachineState.Hibernated
                    and machine.ready
                    for machine in machines
                )
                - max(unit.stopped - stopped[unit.unit_id], 0),
            ),
        )
        for unit in market_units
    )
    hibernation_committed = _hibernation_commitments(market_units, machines, stopped)
    hibernated_committed = _total(
        unit.machine * hibernation_committed[unit.unit_id] for unit in market_units
    )
    hibernated_deficit = (hibernated_target - hibernated_committed).clamped()
    if may_grow and not hibernated_deficit.empty:
        purchases, hibernated_deficit, _ = _purchase_growth(
            policy,
            [offer for offer in offers if offer.supports_hibernation],
            hibernated_deficit,
            (),
            reserve=True,
            action_limit=policy.max_growth_actions - sum(action.count for action in growth),
        )
        growth.extend(purchases)
    prepared = _total(
        offer.machine * action.count
        for action in growth
        if action.kind is GrowthKind.Prepare
        for offer in offers
        if offer.key == action.offer_key
    )
    stopped_deficit = (stopped_target - stopped_capacity - prepared).clamped()
    stopped_shapes = tuple(
        shape
        for shape in conditions.request_shapes.get(market, ())
        if not stopped_target.empty
        and not any(stopped[unit.unit_id] and unit.machine.covers(shape) for unit in market_units)
        and not any(
            offer.machine.covers(shape)
            for action in growth
            if action.kind is GrowthKind.Prepare
            for offer in offers
            if offer.key == action.offer_key
        )
    )
    if not stopped_deficit.empty or stopped_shapes:
        if may_grow:
            purchases, stopped_deficit, stopped_shapes = _purchase_growth(
                policy,
                offers,
                stopped_deficit,
                stopped_shapes,
                reserve=True,
                action_limit=policy.max_growth_actions - sum(action.count for action in growth),
            )
            growth.extend(purchases)
    else:
        stopped = _retire_reserves(
            market_units,
            stopped,
            machines=machines,
            stopped_capacity=stopped_capacity,
            target=stopped_target,
            hibernated_target=hibernated_target.upper(hibernated_capacity.lower(stopped_target)),
            shapes=conditions.request_shapes.get(market, ()) if not stopped_target.empty else (),
        )

    stopped_capacity = _total(unit.machine * stopped[unit.unit_id] for unit in market_units)
    stopped_ready = _total(
        unit.machine
        * max(
            0,
            min(
                stopped[unit.unit_id],
                sum(
                    machine.unit_id == unit.unit_id and machine.state.stopped and machine.ready
                    for machine in machines
                )
                - max(unit.stopped - stopped[unit.unit_id], 0),
            ),
        )
        for unit in market_units
    )
    hibernated_capacity = _total(
        unit.machine
        * max(
            0,
            min(
                stopped[unit.unit_id],
                sum(
                    machine.unit_id == unit.unit_id
                    and machine.state is ReserveMachineState.Hibernated
                    and machine.ready
                    for machine in machines
                )
                - max(unit.stopped - stopped[unit.unit_id], 0),
            ),
        )
        for unit in market_units
    )
    rightsize_source, rightsize_offer_key = _rightsize(
        policy,
        [
            replace(machine, protected=True) if machine.key in placement_hosts else machine
            for machine in warm
        ],
        units,
        offers,
        warm_target,
        conditions,
        lightly_used,
        blocked=not may_grow
        or bool(growth)
        or not warm_pending.empty
        or bool(consolidate)
        or market in conditions.consolidating,
        shapes=conditions.request_shapes.get(market, ()),
    )
    if rightsize_offer_key:
        growth.append(ReserveGrowth(GrowthKind.Buy, offer_key=rightsize_offer_key))
    return MarketReservePlan(
        market=market,
        load=load,
        quiet=quiet,
        warm_target=warm_target,
        warm_free=warm_free,
        stopped_target=stopped_target,
        stopped_capacity=stopped_capacity,
        stopped_ready=stopped_ready,
        stopped_pending=(stopped_capacity - stopped_ready).clamped(),
        hibernated_capacity=hibernated_capacity,
        hibernation_requested_capacity=_total(
            unit.machine
            * max(
                0,
                min(
                    stopped[unit.unit_id],
                    sum(
                        machine.unit_id == unit.unit_id
                        and machine.state is ReserveMachineState.HibernationRequested
                        and machine.ready
                        for machine in machines
                    )
                    - max(unit.stopped - stopped[unit.unit_id], 0),
                ),
            )
            for unit in market_units
        ),
        hibernated_target=hibernated_target,
        hibernated_shortfall=hibernated_deficit,
        growth=tuple(growth),
        warm_pending=warm_pending,
        shortfall=pending_deficit,
        stopped_shortfall=stopped_deficit,
        unmet_shapes=shapes,
        unmet_stopped_shapes=stopped_shapes,
        reason="waiting for demand acquisition or recovery"
        if not may_grow
        else "provider batch pending or no approved offer"
        if not pending_deficit.empty
        or not stopped_deficit.empty
        or shapes
        or stopped_shapes
        or unmet_placements
        or not hibernated_deficit.empty
        else "",
        retained=retained,
        stopped=stopped,
        consolidation_candidate=candidate,
        consolidate=consolidate,
        consolidation_destinations=destinations,
        rightsize_source=rightsize_source,
        rightsize_offer_key=rightsize_offer_key,
        unmet_placements=unmet_placements,
    )


def _uncovered_placements(
    demands: tuple[ReserveDemand, ...],
    machines: list[ReserveMachine],
    units: Mapping[str, ReserveUnit],
    planned: tuple[tuple[ReservePlacement, Capacity], ...],
) -> tuple[tuple[ReserveDemand, ...], frozenset[str]]:
    hosts = [
        (
            machine.key,
            units[machine.unit_id].placement,
            (units[machine.unit_id].machine - machine.load).clamped(),
        )
        for machine in machines
        if units[machine.unit_id].enabled
        and (
            machine.state is ReserveMachineState.Starting
            or (machine.state is ReserveMachineState.Serving and machine.ready)
        )
    ]
    hosts.extend(
        ("", unit.placement, unit.machine)
        for unit in units.values()
        if unit.enabled
        for _ in range(_pending_slots(unit, machines))
    )
    hosts.extend(("", placement, capacity) for placement, capacity in planned)
    unmet: list[ReserveDemand] = []
    protected: set[str] = set()
    for demand in sorted(
        demands,
        key=lambda item: (
            sum(
                bool(value)
                for value in (
                    item.placement.region,
                    item.placement.zone,
                    item.placement.architecture,
                    item.placement.runtime,
                )
            ),
            item.capacity.gpu_count,
            item.capacity.memory_mib,
        ),
        reverse=True,
    ):
        remaining = demand.count
        for index, (key, placement, free) in enumerate(hosts):
            if not demand.placement.accepts(placement):
                continue
            count = min(
                [
                    remaining,
                    *(
                        have // need
                        for have, need in (
                            (free.cpu_millicores, demand.capacity.cpu_millicores),
                            (free.memory_mib, demand.capacity.memory_mib),
                            (free.gpu_count, demand.capacity.gpu_count),
                        )
                        if need
                    ),
                ]
            )
            if count:
                hosts[index] = (key, placement, free - demand.capacity * count)
                remaining -= count
                if key:
                    protected.add(key)
            if not remaining:
                break
        if remaining:
            unmet.append(replace(demand, count=remaining))
    return tuple(unmet), frozenset(protected)


def _pending_slots(unit: ReserveUnit, machines: list[ReserveMachine]) -> int:
    return max(
        0,
        unit.desired
        - sum(
            machine.unit_id == unit.unit_id and not machine.state.reserve for machine in machines
        ),
    )


def _rightsize(
    policy: FleetCapacityPolicy,
    warm: list[ReserveMachine],
    units: Mapping[str, ReserveUnit],
    offers: list[ReserveOffer],
    warm_target: Capacity,
    conditions: ReserveConditions,
    lightly_used: dict[str, datetime],
    *,
    blocked: bool,
    shapes: tuple[Capacity, ...],
) -> tuple[str, str]:
    if blocked:
        return "", ""
    choices: list[tuple[int, str, str]] = []
    for machine in warm:
        unit = units[machine.unit_id]
        if (
            machine.containers
            or machine.protected
            or not machine.billing_settled
            or unit.hourly_cost_micros is None
        ):
            continue
        since = lightly_used.get(machine.key, conditions.now)
        if conditions.now - since < timedelta(seconds=policy.consolidation_seconds):
            continue
        others = [other for other in warm if other.key != machine.key]
        free = _total((units[other.unit_id].machine - other.load).clamped() for other in others)
        need = (warm_target - free).clamped()
        if need.empty:
            continue
        for offer in offers:
            if offer.placement != unit.placement or not offer.machine.covers(need):
                continue
            if not all(
                offer.machine.covers(shape)
                or any(
                    (units[other.unit_id].machine - other.load).covers(shape) for other in others
                )
                for shape in shapes
            ):
                continue
            saving = unit.hourly_cost_micros - offer.hourly_cost_micros
            payback = (
                saving * policy.cost_horizon_seconds
                - offer.hourly_cost_micros * policy.provision_seconds
            )
            if saving > 0 and payback > 0:
                choices.append((payback, machine.key, offer.key))
    if not choices:
        return "", ""
    _, source, offer_key = max(choices)
    return source, offer_key


def _purchase_growth(
    policy: FleetCapacityPolicy,
    offers: list[ReserveOffer],
    deficit: Capacity,
    shapes: tuple[Capacity, ...],
    *,
    reserve: bool,
    action_limit: int,
) -> tuple[list[ReserveGrowth], Capacity, tuple[Capacity, ...]]:
    """Compare bounded node combinations by their cost over the holding horizon.

    Coverage is capped at the target before states are deduplicated. Individual
    request shapes also have to fit one host; aggregate capacity cannot prove it.
    """
    if (deficit.empty and not shapes) or action_limit <= 0:
        return [], deficit, shapes
    candidates = sorted(
        (offer for offer in offers if not reserve or offer.supports_reserve),
        key=lambda offer: (offer.preference_rank, offer.key),
    )
    if not candidates:
        return [], deficit, shapes
    # cost, supplied, uncovered shapes, nominal running CPU, selected offers
    states: list[tuple[int, Capacity, tuple[Capacity, ...], int, tuple[int, ...]]] = [
        (0, Capacity(), shapes, 0, ())
    ]
    best: tuple[int, Capacity, tuple[Capacity, ...], int, tuple[int, ...]] | None = None
    partial = states[0]
    for _ in range(action_limit):
        expanded: dict[
            tuple[Capacity, tuple[Capacity, ...], int],
            tuple[int, Capacity, tuple[Capacity, ...], int, tuple[int, ...]],
        ] = {}
        for cost, supplied, unmet, cpu, selected in states:
            for index, offer in enumerate(candidates):
                next_cpu = cpu + offer.nominal_cpu_millicores
                covered = (supplied + offer.machine).lower(deficit)
                remaining = tuple(shape for shape in unmet if not offer.machine.covers(shape))
                if covered == supplied and remaining == unmet:
                    continue
                holding_cost = (
                    offer.stopped_hourly_cost_micros * policy.cost_horizon_seconds
                    + offer.hourly_cost_micros * policy.provision_seconds
                    if reserve
                    else offer.hourly_cost_micros * policy.cost_horizon_seconds
                )
                next_cost = cost + holding_cost
                if best is not None and next_cost >= best[0]:
                    continue
                state = (next_cost, covered, remaining, next_cpu, (*selected, index))
                if covered.covers(deficit) and not remaining:
                    best = state
                    continue
                key = (covered, remaining, next_cpu)
                if key not in expanded or next_cost < expanded[key][0]:
                    expanded[key] = state

        def order(
            state: tuple[int, Capacity, tuple[Capacity, ...], int, tuple[int, ...]],
        ) -> tuple[float, int, tuple[int, ...]]:
            cost, supplied, unmet, _, selected = state
            coverage = (
                sum(
                    have / need
                    for have, need in (
                        (supplied.cpu_millicores, deficit.cpu_millicores),
                        (supplied.memory_mib, deficit.memory_mib),
                        (supplied.gpu_count, deficit.gpu_count),
                    )
                    if need > 0
                )
                + len(shapes)
                - len(unmet)
            )
            return cost / max(coverage, 0.001), cost, selected

        states = sorted(expanded.values(), key=order)[:128]
        if not states:
            break
        partial = max(
            [partial, *states],
            key=lambda item: (
                -len(item[2]),
                item[1].cpu_millicores,
                item[1].memory_mib,
                item[1].gpu_count,
                -item[0],
            ),
        )
    chosen = best or partial
    counts: dict[int, int] = {}
    for index in chosen[4]:
        counts[index] = counts.get(index, 0) + 1
    actions: list[ReserveGrowth] = []
    for index, count in counts.items():
        offer = candidates[index]
        actions.append(
            ReserveGrowth(
                GrowthKind.Prepare if reserve else GrowthKind.Buy,
                offer_key=offer.key,
                count=count,
            )
        )
    return actions, (deficit - chosen[1]).clamped(), chosen[2]


def _retention(
    market_units: list[ReserveUnit],
    warm: list[ReserveMachine],
    units: Mapping[str, ReserveUnit],
    *,
    surplus: Capacity,
    release_largest_first: bool,
    hold_all: bool,
    shapes: tuple[Capacity, ...],
) -> dict[str, int]:
    serving = [machine for machine in warm if machine.state is ReserveMachineState.Serving]
    retained = {unit.unit_id: 0 for unit in market_units}
    idle: list[ReserveMachine] = []
    for machine in serving:
        if machine.containers or machine.protected or hold_all:
            retained[machine.unit_id] += 1
        else:
            idle.append(machine)
    idle.sort(
        key=lambda machine: (
            -(units[machine.unit_id].hourly_cost_micros or 0),
            -units[machine.unit_id].machine.cpu_millicores
            if release_largest_first
            else units[machine.unit_id].machine.cpu_millicores,
            -units[machine.unit_id].machine.memory_mib
            if release_largest_first
            else units[machine.unit_id].machine.memory_mib,
            machine.key,
        )
    )
    remaining = {machine.key: machine for machine in serving}
    feasible_shapes = tuple(
        shape
        for shape in shapes
        if any((units[machine.unit_id].machine - machine.load).covers(shape) for machine in serving)
    )
    for machine in idle:
        capacity = units[machine.unit_id].machine
        if (surplus - capacity).covers(Capacity()) and all(
            any(
                other.key != machine.key
                and (units[other.unit_id].machine - other.load).covers(shape)
                for other in remaining.values()
            )
            for shape in feasible_shapes
        ):
            surplus = surplus - capacity
            del remaining[machine.key]
        else:
            retained[machine.unit_id] += 1
    return retained


def _retire_reserves(
    market_units: list[ReserveUnit],
    stopped: dict[str, int],
    *,
    machines: list[ReserveMachine],
    stopped_capacity: Capacity,
    target: Capacity,
    hibernated_target: Capacity,
    shapes: tuple[Capacity, ...],
) -> dict[str, int]:
    ready = {
        unit.unit_id: max(
            0,
            min(
                stopped[unit.unit_id],
                sum(
                    machine.unit_id == unit.unit_id and machine.ready and machine.state.stopped
                    for machine in machines
                )
                - max(unit.stopped - stopped[unit.unit_id], 0),
            ),
        )
        for unit in market_units
    }
    usable = _total(unit.machine * ready[unit.unit_id] for unit in market_units)
    required_usable = usable.lower(target)
    hibernated = {
        unit.unit_id: max(
            0,
            min(
                stopped[unit.unit_id],
                sum(
                    machine.unit_id == unit.unit_id
                    and machine.ready
                    and machine.state is ReserveMachineState.Hibernated
                    for machine in machines
                )
                - max(unit.stopped - stopped[unit.unit_id], 0),
            ),
        )
        for unit in market_units
    }
    fast = _total(unit.machine * hibernated[unit.unit_id] for unit in market_units)
    required_fast = fast.lower(hibernated_target)
    committed = _hibernation_commitments(market_units, machines, stopped)
    future_fast = _total(unit.machine * committed[unit.unit_id] for unit in market_units)
    required_future_fast = future_fast.lower(hibernated_target)
    protected = {
        unit.unit_id: sum(
            machine.unit_id == unit.unit_id and machine.state.reserve and machine.protected
            for machine in machines
        )
        for unit in market_units
    }
    for unit in sorted(
        market_units,
        key=lambda item: (
            item.growable,
            -(item.stopped_hourly_cost_micros or 0),
            -item.machine.cpu_millicores,
            -item.machine.memory_mib,
            item.unit_id,
        ),
    ):
        while stopped[unit.unit_id] > protected[unit.unit_id] and (
            stopped_capacity - unit.machine
        ).covers(target):
            # Pool targets do not name the retired slot. Preserve coverage even
            # when the provider removes a ready slot before a preparing one.
            removes_ready = ready[unit.unit_id] > 0
            removes_fast = hibernated[unit.unit_id] > 0
            removes_commitment = committed[unit.unit_id] > 0
            if removes_commitment and not (future_fast - unit.machine).covers(required_future_fast):
                break
            if removes_fast and not (fast - unit.machine).covers(required_fast):
                break
            if removes_ready and not (usable - unit.machine).covers(required_usable):
                break
            if any(
                unit.machine.covers(shape)
                and not any(
                    ready[other.unit_id] > int(other.unit_id == unit.unit_id)
                    and other.machine.covers(shape)
                    for other in market_units
                )
                for shape in shapes
            ):
                break
            stopped[unit.unit_id] -= 1
            stopped_capacity = stopped_capacity - unit.machine
            if removes_ready:
                ready[unit.unit_id] -= 1
                usable -= unit.machine
            if removes_fast:
                hibernated[unit.unit_id] -= 1
                fast -= unit.machine
            if removes_commitment:
                committed[unit.unit_id] -= 1
                future_fast -= unit.machine
    return stopped


def _hibernation_commitments(
    units: list[ReserveUnit], machines: list[ReserveMachine], stopped: Mapping[str, int]
) -> dict[str, int]:
    commitments: dict[str, int] = {}
    for unit in units:
        observed = [
            machine
            for machine in machines
            if machine.unit_id == unit.unit_id and machine.state.reserve
        ]
        # An accepted plain stop may follow a refused hibernation request. Keep
        # that slot until useful demand consumes it instead of retrying with new hosts.
        committed = sum(
            machine.state
            in {ReserveMachineState.Hibernated, ReserveMachineState.HibernationRequested}
            or unit.supports_hibernation
            for machine in observed
        )
        if unit.supports_hibernation:
            committed += max(unit.stopped - len(observed), 0)
        commitments[unit.unit_id] = max(
            0, min(stopped[unit.unit_id], committed - max(unit.stopped - stopped[unit.unit_id], 0))
        )
    return commitments


def _consolidation(
    policy: FleetCapacityPolicy,
    market: ReserveMarket,
    warm: list[ReserveMachine],
    units: Mapping[str, ReserveUnit],
    *,
    warm_free: Capacity,
    warm_target: Capacity,
    conditions: ReserveConditions,
    lightly_used: dict[str, datetime],
    blocked: bool,
) -> tuple[str, str, tuple[str, ...]]:
    serving = [machine for machine in warm if machine.state is ReserveMachineState.Serving]
    light: list[ReserveMachine] = []
    for machine in serving:
        capacity = units[machine.unit_id].machine
        if not machine.pinned and _lightly_used(
            machine.load, capacity, policy.consolidation_percent
        ):
            lightly_used[machine.key] = conditions.lightly_used_since.get(
                machine.key, conditions.now
            )
            if machine.containers:
                light.append(machine)
    if blocked or len(serving) < 2:
        return "", "", ()
    # Moving a machine's work takes its free room away and spends the rest of
    # the market's on the work it moves, so what remains must still meet the target.
    candidates = [
        machine
        for machine in light
        if not machine.protected
        and machine.billing_settled
        and (warm_free - units[machine.unit_id].machine).covers(warm_target)
        and any(
            other.key != machine.key
            and not other.protected
            and units[other.unit_id].placement == units[machine.unit_id].placement
            and (units[other.unit_id].machine - other.load).covers(machine.load)
            for other in serving
        )
        and all(
            any(
                other.key != machine.key
                and (units[other.unit_id].machine - other.load - machine.load).covers(shape)
                for other in serving
            )
            for shape in conditions.request_shapes.get(market, ())
        )
    ]
    if not candidates:
        return "", "", ()
    candidate = min(
        candidates,
        key=lambda machine: (
            -(units[machine.unit_id].hourly_cost_micros or 0),
            machine.load.cpu_millicores,
            machine.load.memory_mib,
            machine.key,
        ),
    )
    since = lightly_used[candidate.key]
    ready = conditions.now - since >= timedelta(seconds=policy.consolidation_seconds)
    destination = min(
        (
            other
            for other in serving
            if other.key != candidate.key
            and not other.protected
            and units[other.unit_id].placement == units[candidate.unit_id].placement
            and (units[other.unit_id].machine - other.load).covers(candidate.load)
        ),
        key=lambda machine: (
            (units[machine.unit_id].machine - machine.load - candidate.load).memory_mib,
            machine.key,
        ),
    )
    return candidate.key, candidate.key if ready else "", (destination.key,) if ready else ()


def _lightly_used(load: Capacity, capacity: Capacity, percent: int) -> bool:
    return (
        load.cpu_millicores * 100 <= capacity.cpu_millicores * percent
        and load.memory_mib * 100 <= capacity.memory_mib * percent
        and load.gpu_count * 100 <= capacity.gpu_count * percent
    )


__all__ = [
    "FleetCapacityPolicy",
    "FleetReservePlan",
    "FleetReserveSnapshot",
    "GrowthKind",
    "HeadroomTarget",
    "MarketReserve",
    "MarketReservePlan",
    "ReserveConditions",
    "ReserveGrowth",
    "ReserveMachine",
    "ReserveMachineState",
    "ReserveUnit",
    "plan_market_reserve",
]
