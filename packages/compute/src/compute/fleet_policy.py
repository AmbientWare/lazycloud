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
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum

from pydantic import Field, model_validator
from shared.contracts import ContractModel
from shared.gpu import GpuType, normalize_gpu_type

from compute.fleet_resources import Capacity, ReserveMarket, ReserveOffer

RESERVE_PLAN_INTERVAL_SECONDS = 60
RESERVE_EARLY_PLAN_INTERVAL_SECONDS = 20


def _total(items: Iterable[Capacity]) -> Capacity:
    result = Capacity()
    for item in items:
        result = result + item
    return result


class HeadroomTarget(ContractModel):
    """Spare capacity a market keeps: a floor, a share of its load, and a ceiling."""

    floor: Capacity = Capacity()
    load_percent: int = Field(default=0, ge=0, le=100)
    maximum: Capacity = Capacity()

    @model_validator(mode="after")
    def maximum_covers_floor(self) -> HeadroomTarget:
        if not self.maximum.covers(self.floor):
            raise ValueError("a headroom maximum cannot be below its floor")
        return self

    def target(self, load: Capacity) -> Capacity:
        return self.floor.upper(load.percent(self.load_percent)).lower(self.maximum)


class MarketReserve(ContractModel):
    """Running headroom a market keeps warm, and headroom it keeps as stopped machines."""

    warm: HeadroomTarget = HeadroomTarget()
    stopped: HeadroomTarget = HeadroomTarget()


_GIB = 1024

_SPOT_RESERVE = MarketReserve(
    warm=HeadroomTarget(
        floor=Capacity(12_000, 24 * _GIB),
        load_percent=25,
        maximum=Capacity(64_000, 256 * _GIB),
    ),
    stopped=HeadroomTarget(
        floor=Capacity(28_000, 100 * _GIB),
        load_percent=50,
        maximum=Capacity(96_000, 384 * _GIB),
    ),
)
_ON_DEMAND_RESERVE = MarketReserve(
    warm=HeadroomTarget(
        floor=Capacity(6_000, 12 * _GIB),
        load_percent=25,
        maximum=Capacity(64_000, 256 * _GIB),
    ),
    stopped=HeadroomTarget(
        floor=Capacity(35_000, 112 * _GIB),
        load_percent=50,
        maximum=Capacity(96_000, 384 * _GIB),
    ),
)
_ONE_CARD_RESERVE = MarketReserve(
    warm=HeadroomTarget(load_percent=25, maximum=Capacity(gpu_count=4)),
    stopped=HeadroomTarget(
        floor=Capacity(gpu_count=1), load_percent=50, maximum=Capacity(gpu_count=4)
    ),
)


class FleetCapacityPolicy(ContractModel):
    minimum_purchase_margin_percent: int = Field(default=30, ge=0, lt=100)
    max_cpu_instances: int = Field(default=50, ge=0)
    max_gpu_instances: int = Field(default=20, ge=0)
    max_running_cpu_millicores: int = Field(default=512_000, ge=0)
    """Nominal vCPU the platform CPU fleet may run at once, stopped machines excluded."""

    resume_seconds: int = Field(default=30, gt=0)
    provision_seconds: int = Field(default=300, gt=0)
    cost_horizon_seconds: int = Field(default=3600, gt=0)
    max_growth_actions: int = Field(default=16, gt=0)
    maintenance_fraction_percent: int = Field(default=20, ge=1, le=100)
    """Upper bound on elective overlap; resource and cost admission may allow less."""
    max_temporary_hourly_cost_micros: int = Field(default=5_000_000, ge=0)
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

    def machine_limit(self, *, gpu: bool) -> int:
        return self.max_gpu_instances if gpu else self.max_cpu_instances

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

    Reserve = "reserve"
    """Preparing, stopping or stopped: capacity a resume turns into running headroom."""


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
    stopped_resumable: bool = False
    """A stopped reserve a resume would start, rather than one still preparing."""
    ready: bool = True
    """Fresh compatible intake, or verified compatible preparation for a stopped host."""


@dataclass(frozen=True, slots=True)
class FleetReserveSnapshot:
    units: tuple[ReserveUnit, ...]
    machines: tuple[ReserveMachine, ...]
    committed_cpu_machines: int
    committed_gpu_machines: int
    running_cpu_millicores: int
    """Nominal CPU of the running CPU fleet, the figure the vCPU cap limits."""
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
    budget = _GrowthBudget(
        cpu_machines=policy.max_cpu_instances - snapshot.committed_cpu_machines,
        gpu_machines=policy.max_gpu_instances - snapshot.committed_gpu_machines,
        running_cpu_millicores=policy.max_running_cpu_millicores - snapshot.running_cpu_millicores,
    )
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
            budget=budget,
            lightly_used=lightly_used,
            offers=[offer for offer in snapshot.offers if offer.market == market],
        )
        plans.append(plan)
    return FleetReservePlan(markets=tuple(plans), lightly_used_since=lightly_used)


@dataclass(slots=True)
class _GrowthBudget:
    cpu_machines: int
    gpu_machines: int
    running_cpu_millicores: int

    def allows(self, unit_nominal_cpu: int, *, gpu: bool, new_machine: bool, running: bool) -> bool:
        if new_machine and (self.gpu_machines if gpu else self.cpu_machines) < 1:
            return False
        return gpu or not running or self.running_cpu_millicores >= unit_nominal_cpu

    def spend(self, unit_nominal_cpu: int, *, gpu: bool, new_machine: bool, running: bool) -> None:
        if new_machine:
            if gpu:
                self.gpu_machines -= 1
            else:
                self.cpu_machines -= 1
        if running and not gpu:
            self.running_cpu_millicores -= unit_nominal_cpu


def _plan_market(
    policy: FleetCapacityPolicy,
    market: ReserveMarket,
    market_units: list[ReserveUnit],
    machines: list[ReserveMachine],
    units: Mapping[str, ReserveUnit],
    *,
    conditions: ReserveConditions,
    budget: _GrowthBudget,
    lightly_used: dict[str, datetime],
    offers: list[ReserveOffer],
) -> MarketReservePlan:
    reserve = policy.reserve(market)
    warm = [
        machine
        for machine in machines
        if machine.state is ReserveMachineState.Serving
        and machine.ready
        and units[machine.unit_id].enabled
    ]
    working = [machine for machine in machines if machine.state is not ReserveMachineState.Reserve]
    load = _total(machine.load for machine in working)
    launching = _total(
        unit.machine
        * max(
            unit.desired
            - sum(
                machine.unit_id == unit.unit_id and machine.state is not ReserveMachineState.Reserve
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
    warm_target = reserve.warm.target(load).upper(
        conditions.forecast_warm.get(market, Capacity()).lower(reserve.warm.maximum)
    )
    total_target = (reserve.stopped.target(load) + warm_target).upper(
        conditions.forecast_total.get(market, Capacity())
    )
    stopped_target = (total_target - warm_target).clamped().lower(reserve.stopped.maximum)
    if market.preemptible and not market.gpu_type:
        # An interrupted Spot machine's work must fit the reserves that replace it.
        for machine in working:
            stopped_target = stopped_target.upper(machine.load)
    quiet = reserve.warm.floor.covers(load)
    deficit = (warm_target - warm_free).clamped()
    may_grow = market not in conditions.demand and market not in conditions.recovering

    growth: list[ReserveGrowth] = []
    pending_deficit = (deficit - warm_pending).clamped()
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
    )

    retained = _retention(
        market_units,
        warm,
        units,
        surplus=warm_free - warm_target,
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
            sum(
                machine.unit_id == unit.unit_id and machine.state is ReserveMachineState.Reserve
                for machine in machines
            ),
        )
        for unit in market_units
    }
    stopped_capacity = _total(unit.machine * stopped[unit.unit_id] for unit in market_units)
    if may_grow:
        for unit in sorted(
            market_units,
            key=lambda item: (
                item.hourly_cost_micros is None,
                item.hourly_cost_micros or 0,
                item.unit_id,
            ),
        ):
            available = sum(
                machine.unit_id == unit.unit_id and machine.stopped_resumable and machine.ready
                for machine in machines
            )
            while (
                available
                and unit.growable
                and (not pending_deficit.empty or shapes)
                and budget.allows(
                    unit.nominal_cpu_millicores,
                    gpu=bool(market.gpu_type),
                    new_machine=False,
                    running=True,
                )
                and sum(action.count for action in growth) < policy.max_growth_actions
            ):
                if pending_deficit.empty and not any(
                    unit.machine.covers(shape) for shape in shapes
                ):
                    break
                growth.append(ReserveGrowth(GrowthKind.Resume, unit_id=unit.unit_id))
                budget.spend(
                    unit.nominal_cpu_millicores,
                    gpu=bool(market.gpu_type),
                    new_machine=False,
                    running=True,
                )
                pending_deficit = (pending_deficit - unit.machine).clamped()
                shapes = tuple(shape for shape in shapes if not unit.machine.covers(shape))
                available -= 1
                stopped[unit.unit_id] -= 1
                stopped_capacity = (stopped_capacity - unit.machine).clamped()
        purchases, pending_deficit, shapes = _purchase_growth(
            policy,
            offers,
            pending_deficit,
            shapes,
            budget=budget,
            reserve=False,
            action_limit=policy.max_growth_actions - sum(action.count for action in growth),
        )
        growth.extend(purchases)
    stopped_deficit = (stopped_target - stopped_capacity).clamped()
    stopped_shapes = tuple(
        shape
        for shape in conditions.request_shapes.get(market, ())
        if not stopped_target.empty
        and not any(stopped[unit.unit_id] and unit.machine.covers(shape) for unit in market_units)
    )
    if not stopped_deficit.empty or stopped_shapes:
        if may_grow:
            purchases, stopped_deficit, stopped_shapes = _purchase_growth(
                policy,
                offers,
                stopped_deficit,
                stopped_shapes,
                budget=budget,
                reserve=True,
                action_limit=policy.max_growth_actions - sum(action.count for action in growth),
            )
            growth.extend(purchases)
    else:
        stopped = _retire_reserves(
            market_units,
            stopped,
            stopped_capacity=stopped_capacity,
            target=stopped_target,
            shapes=conditions.request_shapes.get(market, ()) if not stopped_target.empty else (),
        )

    candidate, consolidate = _consolidation(
        policy,
        market,
        warm,
        units,
        warm_free=warm_free,
        warm_target=warm_target,
        conditions=conditions,
        lightly_used=lightly_used,
        blocked=not deficit.empty or market in conditions.consolidating,
    )
    return MarketReservePlan(
        market=market,
        load=load,
        quiet=quiet,
        warm_target=warm_target,
        warm_free=warm_free,
        stopped_target=stopped_target,
        stopped_capacity=stopped_capacity,
        growth=tuple(growth),
        warm_pending=warm_pending,
        shortfall=pending_deficit,
        stopped_shortfall=stopped_deficit,
        unmet_shapes=shapes,
        unmet_stopped_shapes=stopped_shapes,
        reason="waiting for demand acquisition or recovery"
        if not may_grow
        else "capacity limit or no approved offer"
        if not pending_deficit.empty or not stopped_deficit.empty or shapes or stopped_shapes
        else "",
        retained=retained,
        stopped=stopped,
        consolidation_candidate=candidate,
        consolidate=consolidate,
    )


def _purchase_growth(
    policy: FleetCapacityPolicy,
    offers: list[ReserveOffer],
    deficit: Capacity,
    shapes: tuple[Capacity, ...],
    *,
    budget: _GrowthBudget,
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
    gpu = bool(candidates[0].market.gpu_type)
    count_limit = min(action_limit, budget.gpu_machines if gpu else budget.cpu_machines)
    # cost, supplied, uncovered shapes, nominal running CPU, selected offers
    states: list[tuple[int, Capacity, tuple[Capacity, ...], int, tuple[int, ...]]] = [
        (0, Capacity(), shapes, 0, ())
    ]
    best: tuple[int, Capacity, tuple[Capacity, ...], int, tuple[int, ...]] | None = None
    partial = states[0]
    for _ in range(max(0, count_limit)):
        expanded: dict[
            tuple[Capacity, tuple[Capacity, ...], int],
            tuple[int, Capacity, tuple[Capacity, ...], int, tuple[int, ...]],
        ] = {}
        for cost, supplied, unmet, cpu, selected in states:
            for index, offer in enumerate(candidates):
                next_cpu = cpu + (0 if gpu else offer.nominal_cpu_millicores)
                if next_cpu > budget.running_cpu_millicores and not gpu:
                    continue
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
        for _ in range(count):
            budget.spend(offer.nominal_cpu_millicores, gpu=gpu, new_machine=True, running=True)
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
    stopped_capacity: Capacity,
    target: Capacity,
    shapes: tuple[Capacity, ...],
) -> dict[str, int]:
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
        while stopped[unit.unit_id] and (stopped_capacity - unit.machine).covers(target):
            if any(
                unit.machine.covers(shape)
                and not any(
                    stopped[other.unit_id] > int(other.unit_id == unit.unit_id)
                    and other.machine.covers(shape)
                    for other in market_units
                )
                for shape in shapes
            ):
                break
            stopped[unit.unit_id] -= 1
            stopped_capacity = stopped_capacity - unit.machine
    return stopped


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
) -> tuple[str, str]:
    serving = [machine for machine in warm if machine.state is ReserveMachineState.Serving]
    light: list[ReserveMachine] = []
    for machine in serving:
        capacity = units[machine.unit_id].machine
        if (
            machine.containers
            and not machine.pinned
            and _lightly_used(machine.load, capacity, policy.consolidation_percent)
        ):
            lightly_used[machine.key] = conditions.lightly_used_since.get(
                machine.key, conditions.now
            )
            light.append(machine)
    if blocked or len(serving) < 2:
        return "", ""
    # Moving a machine's work takes its free room away and spends the rest of
    # the market's on the work it moves, so what remains must still meet the target.
    candidates = [
        machine
        for machine in light
        if not machine.protected
        and machine.billing_settled
        and (warm_free - units[machine.unit_id].machine).covers(warm_target)
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
        return "", ""
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
    return candidate.key, candidate.key if ready else ""


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
