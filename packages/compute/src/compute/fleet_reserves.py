"""The fleet as the reserve planner sees it, read from one repository snapshot."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta

from database.repositories.compute import (
    PlatformReserveInstanceRow,
    PlatformReserveRows,
    PlatformReserveUnitRow,
    StoppedReserveUnitRow,
)
from shared.capacity_lifecycle import CapacityImageEvidence, CapacitySleepMode
from shared.compute_enrollment import AgentCapacityState
from shared.compute_fleet import MachineLifecycle
from shared.compute_policy import ComputeUnitPhase
from shared.container_requests import node_memory, schedulable_capacity
from shared.fleet_capacity import ReserveMachineState
from shared.gpu import normalize_gpu_type
from shared.placement import product_region
from shared.releases import ActiveRelease

from compute.fleet_policy import (
    FleetReserveSnapshot,
    ReserveMachine,
    ReserveUnit,
)
from compute.fleet_resources import Capacity, ReserveMarket, ReservePlacement
from compute.offers import ReservationStatus


def unit_reserve_market(*, preemptible: bool, gpu_type: str) -> ReserveMarket:
    return ReserveMarket(
        preemptible=preemptible, gpu_type=normalize_gpu_type(gpu_type) if gpu_type else ""
    )


def machine_capacity(
    cpu_millicores: int, memory_mib: int, gpu_count: int, *, reported_memory_mib: int
) -> Capacity:
    """What one machine of this nominal size gives containers."""
    return Capacity(
        schedulable_capacity(cpu_millicores),
        schedulable_capacity(node_memory(memory_mib, reported_memory_mib)),
        gpu_count,
    )


def _growable(
    unit: PlatformReserveUnitRow, *, purchasable_providers: frozenset[str], now: datetime
) -> bool:
    """Whether a reserve may resume or buy in this unit.

    A unit whose provider refused a launch within its registration timeout keeps
    what it holds and buys nothing more until the cooldown passes.
    """
    if unit.provider_ref not in purchasable_providers:
        return False
    if unit.phase in {ComputeUnitPhase.Deleting, ComputeUnitPhase.Deleted}:
        return False
    cooldown = timedelta(seconds=unit.registration_timeout_seconds)
    if unit.degraded_reason is not None:
        return False
    failed = unit.last_capacity_failure_at
    return failed is None or now >= failed + cooldown


def fleet_reserve_snapshot(
    rows: PlatformReserveRows,
    *,
    purchasable_providers: frozenset[str],
    now: datetime,
    ready_machine_ids: frozenset[str],
    release: ActiveRelease | None,
) -> FleetReserveSnapshot:
    units = tuple(
        ReserveUnit(
            unit_id=unit.id,
            market=unit_reserve_market(preemptible=unit.preemptible, gpu_type=unit.gpu_type),
            machine=machine_capacity(
                unit.cpu_millicores,
                unit.memory_mib,
                unit.gpu_count,
                reported_memory_mib=unit.reported_memory_mib,
            ),
            nominal_cpu_millicores=unit.cpu_millicores,
            desired=unit.desired,
            stopped=unit.stopped,
            growable=_growable(unit, purchasable_providers=purchasable_providers, now=now),
            enabled=unit.provider_ref in purchasable_providers,
            hourly_cost_micros=unit.hourly_cost_micros,
            stopped_hourly_cost_micros=unit.stopped_hourly_cost_micros,
            supports_hibernation=unit.supports_hibernation,
            provider=unit.provider,
            provider_region=unit.region,
            instance_type=unit.instance_type,
            placement=ReservePlacement(
                region=(region.value if (region := product_region(unit.region)) else unit.region),
                zone=unit.availability_zone,
                architecture=unit.architecture,
                runtimes=unit.runtimes,
            ),
        )
        for unit in rows.units
    )
    minimums = {unit.id: unit.billing_minimum_seconds for unit in rows.units}
    machines = tuple(
        machine
        for instance in rows.instances
        if (
            machine := reserve_machine(
                instance,
                minimums[instance.unit_id],
                now=now,
                ready_machine_ids=ready_machine_ids,
                release=release,
            )
        )
        is not None
    )
    committed_cpu = 0
    committed_gpu = 0
    running_cpu = 0
    instances_by_unit: dict[str, list[PlatformReserveInstanceRow]] = defaultdict(list)
    for instance in rows.instances:
        if not instance.cleanup_complete:
            instances_by_unit[instance.unit_id].append(instance)
    for unit in rows.units:
        instances = instances_by_unit[unit.id]
        surge = int(bool(unit.replacement_machine_id)) + unit.maintenance_surge_machines
        committed = max(
            unit.desired
            + unit.stopped
            + unit.retiring_stopped
            + surge
            + sum(
                item.status == ReservationStatus.Terminating.value and not item.surge_covered
                for item in instances
            ),
            unit.observed,
            len(instances),
            unit.provider_committed,
        )
        if unit.gpu_count:
            committed_gpu += committed
        else:
            committed_cpu += committed
            stopped = sum(item.status == ReservationStatus.Stopped.value for item in instances)
            running_cpu += max(
                (
                    unit.desired
                    + int(bool(unit.replacement_machine_id))
                    + max(unit.stopped - stopped - unit.maintenance_reserve_refreshes, 0)
                )
                * unit.cpu_millicores
                + unit.maintenance_running_cpu_millicores,
                (len(instances) - stopped) * unit.cpu_millicores,
            )
    return FleetReserveSnapshot(
        units=units,
        machines=machines,
        committed_cpu_machines=committed_cpu,
        committed_gpu_machines=committed_gpu,
        running_cpu_millicores=running_cpu,
    )


def reserve_machine(
    instance: PlatformReserveInstanceRow,
    billing_minimum_seconds: int | None,
    *,
    now: datetime,
    ready_machine_ids: frozenset[str],
    release: ActiveRelease | None,
) -> ReserveMachine | None:
    if instance.cleanup_complete:
        return None
    status = instance.status
    lifecycle = instance.machine_lifecycle
    if (
        instance.missing
        or status in {ReservationStatus.Terminating.value, ReservationStatus.Deleted.value}
        or lifecycle
        in {
            MachineLifecycle.Terminating,
            MachineLifecycle.Deleted,
        }
    ):
        state = ReserveMachineState.Terminating
    elif status == ReservationStatus.Failed.value or lifecycle is MachineLifecycle.Failed:
        state = ReserveMachineState.Failed
    elif status == ReservationStatus.Stopping.value or lifecycle is MachineLifecycle.Stopping:
        state = ReserveMachineState.Stopping
    elif status == ReservationStatus.Preparing.value:
        state = ReserveMachineState.Preparing
    elif status == ReservationStatus.Stopped.value:
        mode = instance.sleep_accepted_mode or instance.sleep_requested_mode
        state = (
            ReserveMachineState.ImageSaved
            if instance.sleep_attempt_id
            and mode is CapacitySleepMode.Hibernate
            and instance.image_evidence is CapacityImageEvidence.Saved
            else ReserveMachineState.HibernateUnverified
            if mode is CapacitySleepMode.Hibernate
            and instance.image_evidence is not CapacityImageEvidence.Failed
            else ReserveMachineState.Stopped
        )
    elif status in {ReservationStatus.Pending.value, ReservationStatus.Resuming.value}:
        state = ReserveMachineState.Starting
    elif status == ReservationStatus.Active.value:
        if (
            lifecycle
            in {
                MachineLifecycle.Requested,
                MachineLifecycle.Provisioning,
                MachineLifecycle.Booting,
                MachineLifecycle.Joining,
                MachineLifecycle.Resuming,
            }
            or instance.machine_id is None
        ):
            state = ReserveMachineState.Starting
        elif lifecycle is MachineLifecycle.Draining or instance.capacity_state in {
            AgentCapacityState.Draining,
            AgentCapacityState.Preempting,
        }:
            state = ReserveMachineState.Draining
        elif (
            lifecycle is MachineLifecycle.Ready
            and instance.capacity_state is AgentCapacityState.Available
            and instance.machine_id in ready_machine_ids
        ):
            state = ReserveMachineState.Serving
        else:
            state = ReserveMachineState.Unavailable
    else:
        return None
    started = instance.billing_started_at
    return ReserveMachine(
        key=instance.machine_id or f"instance:{instance.instance_id}",
        unit_id=instance.unit_id,
        state=state,
        load=Capacity(
            instance.load_cpu_millicores, instance.load_memory_mib, instance.load_gpu_count
        ),
        containers=instance.containers,
        pinned=instance.pinned,
        protected=instance.protected,
        billing_settled=(
            started is None
            or billing_minimum_seconds is None
            or now >= started + timedelta(seconds=billing_minimum_seconds)
        ),
        ready=(
            release is not None
            and release.target.accepts(
                instance.prepared_worker_image,
                instance.prepared_agent_sha256,
            )
            if status == ReservationStatus.Stopped.value
            else instance.machine_id in ready_machine_ids
        ),
    )


@dataclass(frozen=True, slots=True)
class ReserveAdmission:
    """Which stopped reserves demand may resume.

    Spot-tolerant work may resume an On-Demand reserve only while the On-Demand
    reserves left behind still meet their published target, so a burst of Spot work cannot
    take the reserve a devbox resumes onto.
    """

    prepared: frozenset[str]
    withheld_from_preemptible: frozenset[str]


def reserve_admission(
    rows: Iterable[StoppedReserveUnitRow],
    *,
    targets: Mapping[ReserveMarket, Capacity] | None,
) -> ReserveAdmission:
    units = list(rows)
    held: dict[ReserveMarket, Capacity] = {}
    for unit in units:
        if not unit.resumable_count:
            continue
        market = unit_reserve_market(preemptible=unit.preemptible, gpu_type=unit.gpu_type)
        machine = _reserve_capacity(unit)
        held[market] = held.get(market, Capacity()) + machine * unit.resumable_count
    withheld: set[str] = set()
    for unit in units:
        market = unit_reserve_market(preemptible=unit.preemptible, gpu_type=unit.gpu_type)
        if market.preemptible:
            continue
        machine = _reserve_capacity(unit)
        target = targets.get(market) if targets is not None else None
        if target is None or not (held.get(market, Capacity()) - machine).covers(target):
            withheld.add(unit.id)
    return ReserveAdmission(
        prepared=frozenset(unit.id for unit in units if unit.resumable_count),
        withheld_from_preemptible=frozenset(withheld),
    )


def _reserve_capacity(unit: StoppedReserveUnitRow) -> Capacity:
    return machine_capacity(
        unit.cpu_millicores,
        unit.memory_mib,
        unit.gpu_count,
        reported_memory_mib=unit.reported_memory_mib,
    )


__all__ = [
    "ReserveAdmission",
    "fleet_reserve_snapshot",
    "machine_capacity",
    "reserve_admission",
    "unit_reserve_market",
]
