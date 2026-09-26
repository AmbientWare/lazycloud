"""Offline capacity planning with supplied prices, workloads and lifecycle durations."""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from pydantic import TypeAdapter
from shared.startup import COLD_CONTAINER_START_TARGET_SECONDS

from compute.capacity_acquisition import plan_request_capacity
from compute.demand_forecast import HISTORY_SECONDS, DemandForecast, DemandSample, forecast_demand
from compute.fleet_policy import (
    RESERVE_PLAN_INTERVAL_SECONDS,
    FleetCapacityPolicy,
    FleetReserveSnapshot,
    GrowthKind,
    ReserveConditions,
    ReserveMachine,
    ReserveMachineState,
    ReserveUnit,
    plan_market_reserve,
)
from compute.fleet_resources import Capacity, ReserveMarket, ReserveOffer
from compute.maintenance_policy import MaintenanceBudget, MaintenanceCandidate, plan_maintenance

_EPOCH = datetime(2026, 1, 1, tzinfo=UTC)


@dataclass(frozen=True)
class Workload:
    at: float
    duration: float
    capacity: Capacity
    market: ReserveMarket
    known_at: float | None = None


@dataclass(frozen=True)
class InitialCapacity:
    offer_key: str
    count: int
    stopped: bool = False


@dataclass(frozen=True)
class Timings:
    provision_seconds: float = 180
    resume_seconds: float = 12
    stopped_boot_seconds: float = 90
    stop_seconds: float = 45
    terminate_seconds: float = 30
    container_start_seconds: float = 1
    planning_seconds: float = RESERVE_PLAN_INTERVAL_SECONDS


@dataclass(frozen=True)
class Scenario:
    name: str
    duration_seconds: float
    offers: tuple[ReserveOffer, ...]
    workloads: tuple[Workload, ...]
    initial: tuple[InitialCapacity, ...] = ()
    policy: FleetCapacityPolicy = field(default_factory=FleetCapacityPolicy)
    timings: Timings = Timings()
    price_description: str = "supplied scenario estimates"

    def __post_init__(self) -> None:
        if self.duration_seconds <= 0 or self.timings.planning_seconds <= 0:
            raise ValueError("scenario and planning durations must be positive")
        if any(value < 0 for value in asdict(self.timings).values()):
            raise ValueError("lifecycle durations cannot be negative")
        if len({offer.key for offer in self.offers}) != len(self.offers):
            raise ValueError("scenario offer keys must be unique")
        for workload in self.workloads:
            if workload.at < 0 or workload.duration <= 0:
                raise ValueError("workloads require nonnegative arrival and positive duration")
            if not workload.capacity.covers(Capacity()) or workload.capacity.empty:
                raise ValueError("workloads require positive resources")
            if workload.known_at is not None and not 0 <= workload.known_at <= workload.at:
                raise ValueError("a scheduled workload must be known before its arrival")
        for initial in self.initial:
            if initial.count < 0 or initial.offer_key not in {offer.key for offer in self.offers}:
                raise ValueError("initial capacity must name an offer and nonnegative count")


@dataclass
class _Node:
    key: str
    offer: ReserveOffer
    state: ReserveMachineState
    ready_at: float = 0
    load: Capacity = field(default_factory=Capacity)
    allocations: dict[int, float] = field(default_factory=dict)


@dataclass
class SimulationResult:
    scenario: str
    duration_seconds: float
    price_description: str
    arrivals: int = 0
    started: int = 0
    unfinished: int = 0
    capacity_misses: int = 0
    cold_start_deadline_misses: int = 0
    cold_start_p95_seconds: float = 0
    maximum_start_seconds: float = 0
    estimated_running_cost: float = 0
    estimated_stopped_cost: float = 0
    cpu_packing_fraction: float = 0
    memory_packing_fraction: float = 0
    launches: int = 0
    resumes: int = 0
    terminations: int = 0
    peak_nodes: int = 0
    planner_passes: int = 0
    assumptions: tuple[str, ...] = (
        "Prices and startup durations are supplied estimates, not provider measurements.",
        "Demand uses the production purchase planner with virtual capacity reservations.",
        "Placement excludes network, storage, tenancy and application readiness.",
        "Work runs to completion; migration, failures and warm dispatch are not simulated.",
    )


def simulate(scenario: Scenario) -> SimulationResult:
    """Run the production forecast and reserve planner without provider operations."""
    result = SimulationResult(scenario.name, scenario.duration_seconds, scenario.price_description)
    offers = {offer.key: offer for offer in scenario.offers}
    nodes: dict[str, _Node] = {}
    workloads = sorted(scenario.workloads, key=lambda workload: workload.at)
    pending: dict[int, Workload] = {}
    reservations: dict[int, str] = {}
    history: dict[ReserveMarket, list[DemandSample]] = {}
    latencies: list[float] = []
    misses: set[int] = set()
    serial = 0
    for initial in scenario.initial:
        for _ in range(initial.count):
            serial += 1
            nodes[str(serial)] = _Node(
                str(serial),
                offers[initial.offer_key],
                ReserveMachineState.Reserve if initial.stopped else ReserveMachineState.Serving,
            )
    result.peak_nodes = len(nodes)
    now = 0.0
    cursor = 0
    next_plan = 0.0
    cpu_used = cpu_available = memory_used = memory_available = 0.0
    lightly_used: dict[str, datetime] = {}

    while now < scenario.duration_seconds:
        for key, node in list(nodes.items()):
            for request_id, ends in list(node.allocations.items()):
                if ends <= now:
                    node.load -= workloads[request_id].capacity
                    del node.allocations[request_id]
            if node.ready_at <= now:
                if node.state is ReserveMachineState.Draining:
                    del nodes[key]
                elif node.state is ReserveMachineState.Starting:
                    node.state = ReserveMachineState.Serving
        while cursor < len(workloads) and workloads[cursor].at <= now:
            workload = workloads[cursor]
            pending[cursor] = workload
            history.setdefault(workload.market, []).append(
                DemandSample(_EPOCH + timedelta(seconds=workload.at), workload.capacity)
            )
            result.arrivals += 1
            cursor += 1
        for request_id, workload in list(pending.items()):
            fitting = [
                node
                for node in nodes.values()
                if node.state is ReserveMachineState.Serving
                and node.offer.market == workload.market
                and (request_id not in reservations or node.key == reservations[request_id])
                and (node.offer.machine - node.load).covers(workload.capacity)
            ]
            if not fitting:
                misses.add(request_id)
                continue
            node = min(
                fitting,
                key=lambda item: (
                    (item.offer.machine.cpu_millicores - item.load.cpu_millicores)
                    / max(item.offer.machine.cpu_millicores, 1)
                    + (item.offer.machine.memory_mib - item.load.memory_mib)
                    / max(item.offer.machine.memory_mib, 1),
                    item.key,
                ),
            )
            node.load += workload.capacity
            latency = now - workload.at + scenario.timings.container_start_seconds
            node.allocations[request_id] = (
                now + scenario.timings.container_start_seconds + workload.duration
            )
            latencies.append(latency)
            result.started += 1
            result.cold_start_deadline_misses += int(latency >= COLD_CONTAINER_START_TARGET_SECONDS)
            del pending[request_id]
            reservations.pop(request_id, None)

        serial = _acquire_pending(
            scenario, nodes, pending, reservations, result, now=now, serial=serial
        )

        if now >= next_plan:
            timestamp = _EPOCH + timedelta(seconds=now)
            forecasts: dict[ReserveMarket, DemandForecast] = {}
            markets = set(scenario.policy.markets()) | {offer.market for offer in scenario.offers}
            for market in markets:
                history[market] = [
                    sample
                    for sample in history.get(market, [])
                    if sample.observed_at > timestamp - timedelta(seconds=HISTORY_SECONDS)
                ]
                queued = Capacity()
                for workload in pending.values():
                    if workload.market == market:
                        queued += workload.capacity
                forecasts[market] = forecast_demand(
                    history[market],
                    now=timestamp,
                    resume_seconds=scenario.policy.warm_forecast_seconds,
                    provision_seconds=scenario.policy.total_forecast_seconds,
                    pending=queued,
                    scheduled=(
                        DemandSample(_EPOCH + timedelta(seconds=workload.at), workload.capacity)
                        for workload in workloads
                        if workload.market == market
                        and workload.known_at is not None
                        and workload.known_at <= now < workload.at
                    ),
                )
            plan = plan_market_reserve(
                scenario.policy,
                _snapshot(nodes, scenario.offers, now),
                ReserveConditions(
                    now=timestamp,
                    lightly_used_since=lightly_used,
                    demand=frozenset(workload.market for workload in pending.values()),
                    forecast_warm={market: forecast.warm for market, forecast in forecasts.items()},
                    forecast_total={
                        market: forecast.total for market, forecast in forecasts.items()
                    },
                    request_shapes={
                        market: forecast.request_shapes for market, forecast in forecasts.items()
                    },
                ),
            )
            lightly_used = dict(plan.lightly_used_since)
            for market_plan in plan.markets:
                for unit_id, retained in market_plan.retained.items():
                    serving = [
                        node
                        for node in nodes.values()
                        if node.offer.key == unit_id and node.state is ReserveMachineState.Serving
                    ]
                    idle = [node for node in serving if not node.allocations]
                    for node in idle[: max(0, len(serving) - retained)]:
                        node.state = ReserveMachineState.Draining
                        node.ready_at = now + scenario.timings.terminate_seconds
                        result.terminations += 1
                for unit_id, retained in market_plan.stopped.items():
                    stopped = [
                        node
                        for node in nodes.values()
                        if node.offer.key == unit_id and node.state is ReserveMachineState.Reserve
                    ]
                    resumed = sum(
                        action.count
                        for action in market_plan.growth
                        if action.kind is GrowthKind.Resume and action.unit_id == unit_id
                    )
                    for node in stopped[: max(0, len(stopped) - retained - resumed)]:
                        if node.ready_at > now:
                            continue
                        node.state = ReserveMachineState.Draining
                        node.ready_at = now + scenario.timings.terminate_seconds
                        result.terminations += 1
                for action in market_plan.growth:
                    if action.kind is GrowthKind.Resume:
                        available = [
                            node
                            for node in nodes.values()
                            if node.offer.key == action.unit_id
                            and node.state is ReserveMachineState.Reserve
                            and node.ready_at <= now
                        ]
                        for node in available[: action.count]:
                            node.state = ReserveMachineState.Starting
                            node.ready_at = now + (
                                scenario.timings.stopped_boot_seconds
                                if node.offer.market.gpu_type
                                else scenario.timings.resume_seconds
                            )
                            result.resumes += 1
                    else:
                        for _ in range(action.count):
                            serial += 1
                            preparing = action.kind is GrowthKind.Prepare
                            nodes[str(serial)] = _Node(
                                str(serial),
                                offers[action.offer_key],
                                ReserveMachineState.Reserve
                                if preparing
                                else ReserveMachineState.Starting,
                                now
                                + scenario.timings.provision_seconds
                                + (scenario.timings.stop_seconds if preparing else 0),
                            )
                            result.launches += 1
            result.planner_passes += 1
            next_plan = now + scenario.timings.planning_seconds
            result.peak_nodes = max(result.peak_nodes, len(nodes))

        future: list[float] = [scenario.duration_seconds, next_plan]
        if cursor < len(workloads):
            future.append(workloads[cursor].at)
        for node in nodes.values():
            if node.ready_at > now:
                future.append(node.ready_at)
            future.extend(node.allocations.values())
        following = min(moment for moment in future if moment > now)
        elapsed = following - now
        for node in nodes.values():
            stopped = node.state is ReserveMachineState.Reserve and node.ready_at <= now
            if stopped:
                result.estimated_stopped_cost += (
                    node.offer.stopped_hourly_cost_micros * elapsed / 3_600_000_000
                )
            else:
                result.estimated_running_cost += (
                    node.offer.hourly_cost_micros * elapsed / 3_600_000_000
                )
            if node.state is ReserveMachineState.Serving:
                cpu_used += node.load.cpu_millicores * elapsed
                memory_used += node.load.memory_mib * elapsed
                cpu_available += node.offer.machine.cpu_millicores * elapsed
                memory_available += node.offer.machine.memory_mib * elapsed
        now = following

    result.capacity_misses = len(misses)
    result.unfinished = len(pending)
    result.cold_start_deadline_misses += sum(
        scenario.duration_seconds - workload.at >= COLD_CONTAINER_START_TARGET_SECONDS
        for workload in pending.values()
    )
    if latencies:
        ordered = sorted(latencies)
        result.cold_start_p95_seconds = ordered[
            min(len(ordered) - 1, (len(ordered) * 95 + 99) // 100 - 1)
        ]
        result.maximum_start_seconds = ordered[-1]
    result.cpu_packing_fraction = cpu_used / cpu_available if cpu_available else 0
    result.memory_packing_fraction = memory_used / memory_available if memory_available else 0
    return result


def _acquire_pending(
    scenario: Scenario,
    nodes: dict[str, _Node],
    pending: dict[int, Workload],
    reservations: dict[int, str],
    result: SimulationResult,
    *,
    now: float,
    serial: int,
) -> int:
    reserved: dict[str, Capacity] = {}
    for request_id, node_id in reservations.items():
        reserved[node_id] = reserved.get(node_id, Capacity()) + pending[request_id].capacity
    for request_id, workload in pending.items():
        if request_id in reservations:
            continue
        for node in nodes.values():
            if (
                node.state is ReserveMachineState.Starting
                and node.offer.market == workload.market
                and (node.offer.machine - reserved.get(node.key, Capacity())).covers(
                    workload.capacity
                )
            ):
                reservations[request_id] = node.key
                reserved[node.key] = reserved.get(node.key, Capacity()) + workload.capacity
                break
    for market in sorted({workload.market for workload in pending.values()}):
        waiting: list[tuple[int, Workload]] = [
            (request_id, workload)
            for request_id, workload in pending.items()
            if request_id not in reservations and workload.market == market
        ]
        for node in sorted(nodes.values(), key=lambda item: item.offer.hourly_cost_micros):
            if (
                not waiting
                or node.offer.market != market
                or node.state is not ReserveMachineState.Reserve
                or node.ready_at > now
            ):
                continue
            running_cpu = _snapshot(nodes, scenario.offers, now).running_cpu_millicores
            if (
                not market.gpu_type
                and running_cpu + node.offer.nominal_cpu_millicores
                > scenario.policy.max_running_cpu_millicores
            ):
                continue
            free = node.offer.machine
            for request_id, workload in waiting:
                if free.covers(workload.capacity):
                    reservations[request_id] = node.key
                    free -= workload.capacity
            if free == node.offer.machine:
                continue
            node.state = ReserveMachineState.Starting
            node.ready_at = now + (
                scenario.timings.stopped_boot_seconds
                if market.gpu_type
                else scenario.timings.resume_seconds
            )
            result.resumes += 1
            waiting = [(key, workload) for key, workload in waiting if key not in reservations]
        if not waiting:
            continue
        snapshot = _snapshot(nodes, scenario.offers, now)
        machine_limit = (
            scenario.policy.max_gpu_instances - snapshot.committed_gpu_machines
            if market.gpu_type
            else scenario.policy.max_cpu_instances - snapshot.committed_cpu_machines
        )
        offers = tuple(offer for offer in scenario.offers if offer.market == market)
        plan = plan_request_capacity(
            offers,
            [workload.capacity for _, workload in waiting[:100]],
            machine_limit=machine_limit,
            running_cpu_millicores=max(
                0, scenario.policy.max_running_cpu_millicores - snapshot.running_cpu_millicores
            ),
        )
        by_key = {offer.key: offer for offer in offers}
        for planned in plan.nodes:
            serial += 1
            key = str(serial)
            nodes[key] = _Node(
                key,
                by_key[planned.offer_key],
                ReserveMachineState.Starting,
                now + scenario.timings.provision_seconds,
            )
            for index in planned.request_indices:
                reservations[waiting[index][0]] = key
            result.launches += 1
    result.peak_nodes = max(result.peak_nodes, len(nodes))
    return serial


def _snapshot(
    nodes: dict[str, _Node], offers: tuple[ReserveOffer, ...], now: float
) -> FleetReserveSnapshot:
    units: list[ReserveUnit] = []
    for offer in offers:
        members = [node for node in nodes.values() if node.offer.key == offer.key]
        units.append(
            ReserveUnit(
                unit_id=offer.key,
                market=offer.market,
                machine=offer.machine,
                nominal_cpu_millicores=offer.nominal_cpu_millicores,
                desired=sum(node.state is not ReserveMachineState.Reserve for node in members),
                stopped=sum(node.state is ReserveMachineState.Reserve for node in members),
                growable=True,
                hourly_cost_micros=offer.hourly_cost_micros,
                stopped_hourly_cost_micros=offer.stopped_hourly_cost_micros,
            )
        )
    return FleetReserveSnapshot(
        units=tuple(units),
        machines=tuple(
            ReserveMachine(
                key=node.key,
                unit_id=node.offer.key,
                state=node.state,
                load=node.load,
                containers=len(node.allocations),
                pinned=len(node.allocations),
                stopped_resumable=node.state is ReserveMachineState.Reserve
                and node.ready_at <= now,
                ready=node.ready_at <= now,
            )
            for node in nodes.values()
        ),
        committed_cpu_machines=sum(not node.offer.market.gpu_type for node in nodes.values()),
        committed_gpu_machines=sum(bool(node.offer.market.gpu_type) for node in nodes.values()),
        running_cpu_millicores=sum(
            node.offer.nominal_cpu_millicores
            for node in nodes.values()
            if not node.offer.market.gpu_type
            and not (node.state is ReserveMachineState.Reserve and node.ready_at <= now)
        ),
        offers=offers,
    )


def named_scenario(name: str) -> Scenario:
    """Deterministic workloads with synthetic prices, independent of cloud accounts."""
    spot = ReserveMarket(True)
    demand = ReserveMarket(False)
    t4 = ReserveMarket(False, "T4")
    l4 = ReserveMarket(False, "L4")
    cpu_offers = tuple(
        ReserveOffer(
            f"{market.key}/{label}", market, capacity, capacity.cpu_millicores, price, disk, True
        )
        for market in (spot, demand)
        for label, capacity, price, disk in (
            ("small", Capacity(6_000, 12_288), 100_000, 10_000),
            ("large", Capacity(28_000, 102_400), 350_000, 30_000),
            ("memory", Capacity(14_000, 204_800), 400_000, 50_000),
        )
    )
    offers = (
        *cpu_offers,
        ReserveOffer("t4", t4, Capacity(6_000, 24_576, 1), 8_000, 550_000, 15_000, True),
        ReserveOffer("l4", l4, Capacity(6_000, 24_576, 1), 8_000, 750_000, 15_000, True),
    )
    initial = (InitialCapacity("spot:cpu/small", 2), InitialCapacity("spot:cpu/large", 1, True))
    policy = FleetCapacityPolicy()
    if name == "quiet":
        workloads = tuple(
            Workload(60 + index * 180, 30, Capacity(1_000, 2_048), spot) for index in range(8)
        )
    elif name in {"burst", "scheduled-burst"}:
        workloads = tuple(
            Workload(
                300 + index // 20,
                240,
                Capacity(2_000, 4_096),
                spot,
                known_at=0 if name == "scheduled-burst" else None,
            )
            for index in range(100)
        )
    elif name == "memory":
        workloads = tuple(
            Workload(300 + index * 30, 300, Capacity(2_000, 150_000), demand) for index in range(6)
        )
    elif name == "mixed-gpu":
        workloads = tuple(
            Workload(300 + index * 10, 180, Capacity(2_000, 8_192, 1), t4 if index % 2 else l4)
            for index in range(8)
        )
    elif name in {"fleet-100", "fleet-1000"}:
        count = int(name.split("-")[1])
        initial = (InitialCapacity("spot:cpu/small", count),)
        workloads = tuple(Workload(0, 600, Capacity(6_000, 12_288), spot) for _ in range(count))
        policy = policy.model_copy(
            update={
                "max_cpu_instances": count + 50,
                "max_running_cpu_millicores": (count + 50) * 32_000,
            }
        )
    else:
        raise ValueError(f"unknown scenario: {name}")
    return Scenario(
        name,
        1_800,
        offers,
        workloads,
        initial,
        policy,
        price_description="synthetic USD hourly estimates, scenario version 1",
    )


@dataclass(frozen=True)
class RolloutScenario:
    name: str
    machines: int
    stopped: bool
    demand_spike: bool = False
    failures: int = 0
    operation_seconds: float = 180
    failure_cleanup_seconds: float = 120
    retry_seconds: float = 60
    machine_hourly_cost_micros: int = 100_000
    maximum_hourly_cost_micros: int = 5_000_000
    maximum_running_cpu_millicores: int = 512_000
    fraction_percent: int = 20


@dataclass
class _MaintenanceOperation:
    candidate: MaintenanceCandidate
    finishes_at: float
    failed: bool


@dataclass
class RolloutResult:
    scenario: str
    machines: int
    completed: int = 0
    duration_seconds: float = 0
    failed_attempts: int = 0
    peak_concurrent: int = 0
    peak_temporary_machines: int = 0
    peak_temporary_hourly_cost: float = 0
    estimated_temporary_cost: float = 0
    minimum_available_capacity_fraction: float = 1
    peak_during_demand_spike: int = 0
    admitted_during_demand_spike: int = 0
    completed_during_demand_spike: int = 0
    assumptions: tuple[str, ...] = (
        "Maintenance uses the production planner with supplied synthetic costs and times.",
        "An operation retains its capacity and cost reservation through failure cleanup.",
        "Demand raises required headroom; existing operations finish and new ones may pause.",
        "Runtime operations share candidate replacements to exercise exclusive reservations.",
    )


def simulate_rollout(scenario: RolloutScenario) -> RolloutResult:
    result = RolloutResult(scenario.name, scenario.machines)
    market = "reserve:cpu" if scenario.stopped else "warm:cpu"
    capacity = Capacity(8_000, 16_384)
    candidates = {
        str(index): MaintenanceCandidate(
            machine_id=str(index),
            market=market,
            unavailable=capacity,
            surge_machines=int(not scenario.stopped),
            running_cpu_millicores=capacity.cpu_millicores,
            hourly_cost_micros=scenario.machine_hourly_cost_micros,
            replacement_machine_id=None if scenario.stopped else f"replacement-{index // 2}",
        )
        for index in range(scenario.machines)
    }
    active: dict[str, _MaintenanceOperation] = {}
    attempted: set[str] = set()
    retry_at: dict[str, float] = {}
    now = 0.0
    operation_limit = max(1, scenario.machines * scenario.fraction_percent // 100)
    while candidates or active:
        for key, operation in list(active.items()):
            if operation.finishes_at <= now:
                del active[key]
                if operation.failed:
                    result.failed_attempts += 1
                    retry_at[key] = now + scenario.retry_seconds
                else:
                    del candidates[key]
                    result.completed += 1
                    if scenario.demand_spike and 60 <= now < 300:
                        result.completed_during_demand_spike += 1
        spike = scenario.demand_spike and 60 <= now < 300
        required_fraction = 95 if spike else 50
        committed_cost = len(active) * scenario.machine_hourly_cost_micros
        budget = MaintenanceBudget(
            available_operations=operation_limit - len(active),
            available_machines=operation_limit
            - sum(operation.candidate.surge_machines for operation in active.values()),
            available_running_cpu_millicores=scenario.maximum_running_cpu_millicores
            - len(active) * capacity.cpu_millicores,
            available_hourly_cost_micros=scenario.maximum_hourly_cost_micros - committed_cost,
            ready={market: capacity * (scenario.machines - len(active))},
            required_ready={market: (capacity * scenario.machines).percent(required_fraction)},
            reserved_replacements=frozenset(
                operation.candidate.replacement_machine_id
                for operation in active.values()
                if operation.candidate.replacement_machine_id is not None
            ),
        )
        selected = plan_maintenance(
            [
                candidate
                for key, candidate in candidates.items()
                if key not in active and retry_at.get(key, 0) <= now
            ],
            budget,
        )
        if spike:
            result.admitted_during_demand_spike += len(selected)
        for candidate in selected:
            failed = (
                candidate.machine_id not in attempted
                and int(candidate.machine_id) < scenario.failures
            )
            attempted.add(candidate.machine_id)
            active[candidate.machine_id] = _MaintenanceOperation(
                candidate,
                now
                + scenario.operation_seconds
                + (scenario.failure_cleanup_seconds if failed else 0),
                failed,
            )
        result.peak_concurrent = max(result.peak_concurrent, len(active))
        result.peak_temporary_machines = max(
            result.peak_temporary_machines,
            sum(operation.candidate.surge_machines for operation in active.values()),
        )
        result.peak_temporary_hourly_cost = max(
            result.peak_temporary_hourly_cost,
            len(active) * scenario.machine_hourly_cost_micros / 1_000_000,
        )
        result.minimum_available_capacity_fraction = min(
            result.minimum_available_capacity_fraction,
            (scenario.machines - len(active)) / scenario.machines,
        )
        if spike:
            result.peak_during_demand_spike = max(result.peak_during_demand_spike, len(active))
        if not candidates and not active:
            break
        events = [operation.finishes_at for operation in active.values()]
        events.extend(moment for moment in retry_at.values() if moment > now)
        if scenario.demand_spike:
            events.extend(moment for moment in (60.0, 300.0) if moment > now)
        if not events:
            raise ValueError("maintenance budget cannot admit the remaining operations")
        following = min(events)
        result.estimated_temporary_cost += (
            len(active) * scenario.machine_hourly_cost_micros * (following - now) / 3_600_000_000
        )
        now = following
    result.duration_seconds = now
    return result


def named_rollout_scenario(name: str) -> RolloutScenario:
    if name == "rollout-pressure-failure":
        return RolloutScenario(name, 100, False, demand_spike=True, failures=5)
    _, kind, count = name.split("-")
    return RolloutScenario(name, int(count), stopped=kind == "stopped")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument(
        "--scenario",
        choices=(
            "quiet",
            "burst",
            "scheduled-burst",
            "memory",
            "mixed-gpu",
            "fleet-100",
            "fleet-1000",
            "rollout-runtime-100",
            "rollout-runtime-1000",
            "rollout-stopped-100",
            "rollout-stopped-1000",
            "rollout-pressure-failure",
        ),
    )
    source.add_argument(
        "--input",
        type=Path,
        help="JSON Scenario with explicit offers, prices, workloads and timings",
    )
    args = parser.parse_args()
    if args.scenario and args.scenario.startswith("rollout-"):
        print(
            json.dumps(
                asdict(simulate_rollout(named_rollout_scenario(args.scenario))), sort_keys=True
            )
        )
        return
    scenario = (
        TypeAdapter(Scenario).validate_json(args.input.read_text())
        if args.input
        else named_scenario(args.scenario)
    )
    print(json.dumps(asdict(simulate(scenario)), sort_keys=True))


if __name__ == "__main__":
    main()
