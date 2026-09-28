"""Forecast lead times from compatible reserves and observed serving activations."""

from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import datetime

from database.repositories.capacity_activations import CapacityActivationSummary
from shared.capacity_lifecycle import CapacityActivationKind, CapacityRestoreOutcome
from shared.fleet_capacity import ReserveMachineState
from shared.placement import product_region

from compute.demand_forecast import DemandForecast, DemandSample, ForecastTiming, forecast_demand
from compute.fleet_policy import (
    RESERVE_PLAN_INTERVAL_SECONDS,
    FleetCapacityPolicy,
    FleetReserveSnapshot,
    ReserveUnit,
)
from compute.fleet_resources import Capacity, ReserveMarket, ReservePlacement


@dataclass(frozen=True, slots=True)
class _Estimate:
    seconds: float
    evidence: ForecastTiming


def _estimate(
    rows: Sequence[CapacityActivationSummary],
    *,
    kind: CapacityActivationKind,
    fallback: int,
    failure_fallback: int,
    cold_fallback: int,
    planning_seconds: float,
) -> _Estimate:
    matching = [row for row in rows if row.kind is kind]
    ready = sum(row.ready for row in matching)
    failed = sum(row.failed for row in matching)
    cold = sum(
        row.ready for row in matching if row.restore_outcome is CapacityRestoreOutcome.ColdBoot
    )
    fallback_used = (
        not matching
        or failed > 0
        or any(row.ready < 20 or row.p95_ready_seconds is None for row in matching)
    )
    baseline = max(fallback, failure_fallback if failed else 0, cold_fallback if cold else 0)
    observed = [row.p95_ready_seconds for row in matching if row.p95_ready_seconds is not None]
    seconds = max(observed, default=float(baseline))
    if fallback_used or cold:
        seconds = max(seconds, baseline)
    return _Estimate(
        seconds + planning_seconds,
        ForecastTiming(kind, ready, failed, cold, fallback_used or bool(cold)),
    )


def _matches(row: CapacityActivationSummary, unit: ReserveUnit) -> bool:
    return (
        row.gpu_type == unit.market.gpu_type
        and bool(unit.provider)
        and row.provider == unit.provider
        and bool(unit.instance_type)
        and row.instance_type == unit.instance_type
        and row.region == unit.provider_region
        and row.architecture == unit.placement.architecture
    )


def reserve_forecast(
    policy: FleetCapacityPolicy,
    snapshot: FleetReserveSnapshot,
    activations: Sequence[CapacityActivationSummary],
    *,
    market: ReserveMarket,
    history: Sequence[DemandSample],
    scheduled: Sequence[DemandSample],
    pending: Capacity,
    now: datetime,
    placement: ReservePlacement = ReservePlacement(),
    pending_shapes: Sequence[Capacity] = (),
    planning_seconds: float = RESERVE_PLAN_INTERVAL_SECONDS,
) -> DemandForecast:
    units = {unit.unit_id: unit for unit in snapshot.units}
    provision_rows = [
        row
        for row in activations
        if row.gpu_type == market.gpu_type
        and (not placement.region or placement.region == (product_region(row.region) or row.region))
        and (not placement.architecture or placement.architecture == row.architecture)
    ]
    provision = _estimate(
        provision_rows,
        kind=CapacityActivationKind.Provision,
        fallback=policy.provision_seconds,
        failure_fallback=policy.provision_seconds,
        cold_fallback=policy.provision_seconds,
        planning_seconds=planning_seconds,
    )
    full = forecast_demand(
        history,
        now=now,
        resume_seconds=provision.seconds,
        provision_seconds=provision.seconds,
        pending=pending,
        scheduled=scheduled,
    )
    ready = [
        machine
        for machine in snapshot.machines
        if machine.ready
        and not machine.protected
        and machine.state.stopped
        and units[machine.unit_id].growable
        and units[machine.unit_id].enabled
        and units[machine.unit_id].market == market
        and placement.accepts(units[machine.unit_id].placement)
    ]
    shapes = (*full.request_shapes, *pending_shapes)

    def covered(states: frozenset[ReserveMachineState]) -> bool:
        capacity = Capacity()
        hosts = [units[machine.unit_id].machine for machine in ready if machine.state in states]
        for host in hosts:
            capacity += host
        return (
            bool(hosts)
            and capacity.covers(full.warm)
            and all(any(host.covers(shape) for host in hosts) for shape in shapes)
        )

    if covered(frozenset({ReserveMachineState.ImageSaved})):
        selected = [machine for machine in ready if machine.state is ReserveMachineState.ImageSaved]
    elif covered(
        frozenset({ReserveMachineState.ImageSaved, ReserveMachineState.HibernateUnverified})
    ):
        selected = [
            machine for machine in ready if machine.state is not ReserveMachineState.Stopped
        ]
    elif covered(frozenset(machine.state for machine in ready)):
        selected = ready
    else:
        return replace(full, timing=provision.evidence)
    estimates = [
        _estimate(
            [row for row in activations if _matches(row, units[machine.unit_id])],
            kind=CapacityActivationKind.Boot
            if machine.state is ReserveMachineState.Stopped
            else CapacityActivationKind.Resume,
            fallback=policy.resume_seconds
            if machine.state is ReserveMachineState.ImageSaved
            else policy.stopped_boot_seconds,
            failure_fallback=policy.provision_seconds,
            cold_fallback=policy.stopped_boot_seconds,
            planning_seconds=planning_seconds,
        )
        for machine in selected
    ]
    warm = max(estimates, key=lambda estimate: estimate.seconds)
    forecast = forecast_demand(
        history,
        now=now,
        resume_seconds=warm.seconds,
        provision_seconds=max(warm.seconds, provision.seconds),
        pending=pending,
        scheduled=scheduled,
    )
    return replace(forecast, timing=warm.evidence)
