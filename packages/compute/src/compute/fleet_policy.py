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
from shared.container_requests import schedulable_capacity
from shared.contracts import ContractModel
from shared.gpu import GpuType, normalize_gpu_type


@dataclass(frozen=True, slots=True)
class Capacity:
    cpu_millicores: int = 0
    memory_mib: int = 0
    gpu_count: int = 0

    def __add__(self, other: Capacity) -> Capacity:
        return Capacity(
            self.cpu_millicores + other.cpu_millicores,
            self.memory_mib + other.memory_mib,
            self.gpu_count + other.gpu_count,
        )

    def __sub__(self, other: Capacity) -> Capacity:
        return Capacity(
            self.cpu_millicores - other.cpu_millicores,
            self.memory_mib - other.memory_mib,
            self.gpu_count - other.gpu_count,
        )

    def __mul__(self, count: int) -> Capacity:
        return Capacity(
            self.cpu_millicores * count, self.memory_mib * count, self.gpu_count * count
        )

    def covers(self, other: Capacity) -> bool:
        """Whether this is at least `other` in every dimension."""
        return (
            self.cpu_millicores >= other.cpu_millicores
            and self.memory_mib >= other.memory_mib
            and self.gpu_count >= other.gpu_count
        )

    def clamped(self) -> Capacity:
        return self.upper(Capacity())

    def upper(self, other: Capacity) -> Capacity:
        return Capacity(
            max(self.cpu_millicores, other.cpu_millicores),
            max(self.memory_mib, other.memory_mib),
            max(self.gpu_count, other.gpu_count),
        )

    def lower(self, other: Capacity) -> Capacity:
        return Capacity(
            min(self.cpu_millicores, other.cpu_millicores),
            min(self.memory_mib, other.memory_mib),
            min(self.gpu_count, other.gpu_count),
        )

    def percent(self, percent: int) -> Capacity:
        return Capacity(
            -(-self.cpu_millicores * percent // 100),
            -(-self.memory_mib * percent // 100),
            -(-self.gpu_count * percent // 100),
        )

    @property
    def empty(self) -> bool:
        return Capacity().covers(self)


def total(items: Iterable[Capacity]) -> Capacity:
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

SPOT_RESERVE = MarketReserve(
    # Two small machines when quiet, so a start lands on a warm worker.
    warm=HeadroomTarget(
        floor=Capacity(12_000, 24 * _GIB),
        load_percent=25,
        maximum=Capacity(64_000, 256 * _GIB),
    ),
    # One large machine, so an interrupted machine's work resumes onto a reserve.
    stopped=HeadroomTarget(
        floor=Capacity(28_000, 100 * _GIB),
        load_percent=50,
        maximum=Capacity(96_000, 384 * _GIB),
    ),
)
ON_DEMAND_RESERVE = MarketReserve(
    # One small and one large stopped machine: a small devbox and a large one
    # each resume in seconds instead of waiting for a purchase.
    stopped=HeadroomTarget(
        floor=Capacity(35_000, 112 * _GIB),
        load_percent=50,
        maximum=Capacity(96_000, 384 * _GIB),
    ),
)
_ONE_CARD_RESERVE = MarketReserve(
    stopped=HeadroomTarget(
        floor=Capacity(gpu_count=1), load_percent=50, maximum=Capacity(gpu_count=4)
    )
)


class MachineRole(StrEnum):
    """What a reserve buys or prepares: a small machine, a large one, or a GPU host."""

    Small = "small"
    Large = "large"
    Gpu = "gpu"


@dataclass(frozen=True, slots=True, order=True)
class ReserveMarket:
    """A purchase market the fleet keeps headroom in.

    GPU headroom is kept On-Demand. A stopped reserve costs only its disk in
    either market, and an On-Demand one serves Spot-tolerant work as well.
    """

    preemptible: bool
    gpu_type: str = ""

    @property
    def key(self) -> str:
        return f"{'spot' if self.preemptible else 'on-demand'}:{self.gpu_type or 'cpu'}"

    @classmethod
    def parse(cls, key: str) -> ReserveMarket:
        market, _, gpu = key.partition(":")
        return cls(preemptible=market == "spot", gpu_type="" if gpu == "cpu" else gpu)


class FleetCapacityPolicy(ContractModel):
    minimum_purchase_margin_percent: int = Field(default=30, ge=0, lt=100)
    max_cpu_instances: int = Field(default=50, ge=0)
    max_gpu_instances: int = Field(default=20, ge=0)
    max_running_cpu_millicores: int = Field(default=512_000, ge=0)
    """Nominal vCPU the platform CPU fleet may run at once, stopped machines excluded."""

    small_machine_cpu_millicores: int = Field(default=8_000, gt=0)
    small_machine_memory_mib: int = Field(default=16 * _GIB, gt=0)
    """Memory of the smallest small machine, which decides when a shortfall needs a large one."""

    large_machine_cpu_millicores: int = Field(default=32_000, gt=0)
    large_machine_memory_mib_per_cpu: int = Field(default=4 * _GIB, gt=0)
    spot: MarketReserve = SPOT_RESERVE
    on_demand: MarketReserve = ON_DEMAND_RESERVE
    gpu: dict[str, MarketReserve] = Field(
        default_factory=lambda: {
            GpuType.T4.value: _ONE_CARD_RESERVE,
            GpuType.A10G.value: _ONE_CARD_RESERVE,
            GpuType.L4.value: _ONE_CARD_RESERVE,
        }
    )
    """On-Demand reserves per card. A card left out keeps no GPU headroom."""

    pressure_seconds: int = Field(default=60, ge=1)
    """How long running headroom stays short before the planner runs early."""

    consolidation_percent: int = Field(default=30, ge=0, le=100)
    consolidation_seconds: int = Field(default=600, ge=0)
    consolidation_cooldown_seconds: int = Field(default=900, ge=0)

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

    def role(self, *, cpu_millicores: int, memory_mib: int, gpu_count: int) -> MachineRole | None:
        """The role of a machine by its nominal size, or None when it plays none."""
        if gpu_count:
            return MachineRole.Gpu
        if cpu_millicores == self.small_machine_cpu_millicores:
            return MachineRole.Small
        if (
            cpu_millicores == self.large_machine_cpu_millicores
            and memory_mib * 1000 >= cpu_millicores * self.large_machine_memory_mib_per_cpu
        ):
            return MachineRole.Large
        return None


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
    role: MachineRole | None
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


@dataclass(frozen=True, slots=True)
class ReserveMachine:
    key: str
    """The machine id, or the provider instance for one that has not enrolled."""

    unit_id: str
    state: ReserveMachineState
    load: Capacity = Capacity()
    containers: int = 0
    pinned: int = 0
    """Live containers that did not accept interruption, so they cannot be moved."""

    protected: bool = False
    """A recovery source, a recovery replacement, or a planned replacement's pair."""

    billing_settled: bool = True
    stopped_resumable: bool = False
    """A stopped reserve a resume would start, rather than one still preparing."""


@dataclass(frozen=True, slots=True)
class FleetReserveSnapshot:
    units: tuple[ReserveUnit, ...]
    machines: tuple[ReserveMachine, ...]
    committed_cpu_machines: int
    committed_gpu_machines: int
    running_cpu_millicores: int
    """Nominal CPU of the running CPU fleet, the figure the vCPU cap limits."""


class GrowthKind(StrEnum):
    Resume = "resume"
    Buy = "buy"
    Prepare = "prepare"


@dataclass(frozen=True, slots=True)
class ReserveGrowth:
    kind: GrowthKind
    role: MachineRole
    unit_id: str = ""
    """The unit to resume a reserve in; a purchase or preparation picks its offer."""


@dataclass(frozen=True, slots=True)
class MarketReservePlan:
    market: ReserveMarket
    load: Capacity
    quiet: bool
    warm_target: Capacity
    warm_free: Capacity
    stopped_target: Capacity
    stopped_capacity: Capacity
    growth: ReserveGrowth | None = None
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


def plan_market_reserve(
    policy: FleetCapacityPolicy,
    snapshot: FleetReserveSnapshot,
    conditions: ReserveConditions,
) -> FleetReservePlan:
    """Decide growth, retention, stopped reserves and consolidation for every market.

    Pure: the same fleet, policy and conditions give the same plan. Each market
    gets at most one growth action a pass, and a market with waiting work gets
    none, so a request's own purchase is never behind a reserve's.
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
) -> MarketReservePlan:
    reserve = policy.reserve(market)
    warm = [
        machine
        for machine in machines
        if machine.state in {ReserveMachineState.Serving, ReserveMachineState.Starting}
        and units[machine.unit_id].enabled
    ]
    working = [machine for machine in machines if machine.state is not ReserveMachineState.Reserve]
    load = total(machine.load for machine in working)
    launching = total(
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
    warm_free = (
        total((units[machine.unit_id].machine - machine.load).clamped() for machine in warm)
        + launching
    )
    warm_target = reserve.warm.target(load)
    stopped_target = reserve.stopped.target(load)
    if market.preemptible and not market.gpu_type:
        # An interrupted Spot machine's work must fit the reserves that replace it.
        for machine in working:
            stopped_target = stopped_target.upper(machine.load)
    quiet = reserve.warm.floor.covers(load)
    deficit = (warm_target - warm_free).clamped()
    may_grow = market not in conditions.demand and market not in conditions.recovering

    growth: ReserveGrowth | None = None
    if not deficit.empty and may_grow:
        growth = _warm_growth(policy, market, market_units, machines, quiet=quiet, budget=budget)

    retained = _retention(
        market_units,
        warm,
        units,
        surplus=warm_free - warm_target,
        release_largest_first=quiet,
        hold_all=not deficit.empty,
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
    stopped_capacity = total(unit.machine * stopped[unit.unit_id] for unit in market_units)
    stopped_deficit = (stopped_target - stopped_capacity).clamped()
    if not stopped_deficit.empty:
        if growth is None and may_grow:
            growth = _reserve_growth(policy, market, stopped_deficit, budget=budget)
    else:
        stopped = _retire_reserves(
            market_units, stopped, stopped_capacity=stopped_capacity, target=stopped_target
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
        growth=growth,
        retained=retained,
        stopped=stopped,
        consolidation_candidate=candidate,
        consolidate=consolidate,
    )


def _warm_growth(
    policy: FleetCapacityPolicy,
    market: ReserveMarket,
    market_units: list[ReserveUnit],
    machines: list[ReserveMachine],
    *,
    quiet: bool,
    budget: _GrowthBudget,
) -> ReserveGrowth | None:
    gpu = bool(market.gpu_type)
    resumable = [
        unit
        for unit in market_units
        if unit.growable
        and unit.stopped
        and any(
            machine.unit_id == unit.unit_id and machine.stopped_resumable for machine in machines
        )
        and budget.allows(unit.nominal_cpu_millicores, gpu=gpu, new_machine=False, running=True)
    ]
    if resumable:
        # Resuming the smallest reserve keeps a quiet market's warm machines small;
        # under load the largest adds the most headroom for the same start.
        unit = min(
            resumable,
            key=lambda item: (
                (item.machine.cpu_millicores, item.machine.memory_mib)
                if quiet
                else (-item.machine.cpu_millicores, -item.machine.memory_mib),
                item.unit_id,
            ),
        )
        budget.spend(unit.nominal_cpu_millicores, gpu=gpu, new_machine=False, running=True)
        return ReserveGrowth(
            GrowthKind.Resume,
            unit.role or (MachineRole.Gpu if gpu else MachineRole.Small),
            unit.unit_id,
        )
    role = MachineRole.Gpu if gpu else MachineRole.Small if quiet else MachineRole.Large
    cpu = _role_cpu(policy, role)
    if not budget.allows(cpu, gpu=gpu, new_machine=True, running=True):
        return None
    budget.spend(cpu, gpu=gpu, new_machine=True, running=True)
    return ReserveGrowth(GrowthKind.Buy, role)


def _reserve_growth(
    policy: FleetCapacityPolicy,
    market: ReserveMarket,
    deficit: Capacity,
    *,
    budget: _GrowthBudget,
) -> ReserveGrowth | None:
    gpu = bool(market.gpu_type)
    if gpu:
        role = MachineRole.Gpu
    else:
        # A shortfall one small machine closes gets a small one; anything more a
        # large one, so one preparation covers what several small ones would.
        small = Capacity(
            schedulable_capacity(policy.small_machine_cpu_millicores),
            schedulable_capacity(policy.small_machine_memory_mib),
        )
        role = MachineRole.Small if small.covers(deficit) else MachineRole.Large
    if not budget.allows(0, gpu=gpu, new_machine=True, running=False):
        return None
    budget.spend(0, gpu=gpu, new_machine=True, running=False)
    return ReserveGrowth(GrowthKind.Prepare, role)


def _retention(
    market_units: list[ReserveUnit],
    warm: list[ReserveMachine],
    units: Mapping[str, ReserveUnit],
    *,
    surplus: Capacity,
    release_largest_first: bool,
    hold_all: bool,
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
            -units[machine.unit_id].machine.cpu_millicores
            if release_largest_first
            else units[machine.unit_id].machine.cpu_millicores,
            -units[machine.unit_id].machine.memory_mib
            if release_largest_first
            else units[machine.unit_id].machine.memory_mib,
            machine.key,
        )
    )
    for machine in idle:
        capacity = units[machine.unit_id].machine
        if (surplus - capacity).covers(Capacity()):
            surplus = surplus - capacity
        else:
            retained[machine.unit_id] += 1
    return retained


def _retire_reserves(
    market_units: list[ReserveUnit],
    stopped: dict[str, int],
    *,
    stopped_capacity: Capacity,
    target: Capacity,
) -> dict[str, int]:
    for unit in sorted(
        market_units,
        key=lambda item: (
            item.growable,
            -item.machine.cpu_millicores,
            -item.machine.memory_mib,
            item.unit_id,
        ),
    ):
        while stopped[unit.unit_id] and (stopped_capacity - unit.machine).covers(target):
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
    ]
    if not candidates:
        return "", ""
    candidate = min(
        candidates,
        key=lambda machine: (
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


def _role_cpu(policy: FleetCapacityPolicy, role: MachineRole) -> int:
    if role is MachineRole.Large:
        return policy.large_machine_cpu_millicores
    if role is MachineRole.Small:
        return policy.small_machine_cpu_millicores
    return 0


__all__ = [
    "Capacity",
    "FleetCapacityPolicy",
    "FleetReservePlan",
    "FleetReserveSnapshot",
    "GrowthKind",
    "HeadroomTarget",
    "MachineRole",
    "MarketReserve",
    "MarketReservePlan",
    "ReserveConditions",
    "ReserveGrowth",
    "ReserveMachine",
    "ReserveMachineState",
    "ReserveMarket",
    "ReserveUnit",
    "plan_market_reserve",
]
