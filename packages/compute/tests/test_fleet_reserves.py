from dataclasses import replace
from datetime import UTC, datetime

from compute.fleet_policy import (
    FleetCapacityPolicy,
    ReserveConditions,
    ReserveMachineState,
    plan_market_reserve,
)
from compute.fleet_reserves import fleet_reserve_snapshot, machine_capacity, reserve_admission
from compute.fleet_resources import ReserveMarket
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


def test_reserve_borrowing_preserves_dynamic_target_and_compatible_inventory() -> None:
    unit = StoppedReserveUnitRow(
        id="reserve",
        preemptible=False,
        gpu_type="",
        cpu_millicores=8_000,
        memory_mib=16_384,
        reported_memory_mib=16_000,
        gpu_count=0,
        stopped=3,
        resumable_count=1,
    )
    capacity = machine_capacity(8_000, 16_384, 0, reported_memory_mib=16_000)
    market = ReserveMarket(False)
    assert reserve_admission((unit,), targets=None).withheld_from_preemptible == {unit.id}
    assert reserve_admission((unit,), targets={market: capacity}).withheld_from_preemptible == {
        unit.id
    }
    ready = replace(unit, resumable_count=3)
    assert reserve_admission(
        (ready,), targets={market: capacity * 3}
    ).withheld_from_preemptible == {unit.id}
    assert (
        reserve_admission((ready,), targets={market: capacity * 2}).withheld_from_preemptible
        == set()
    )


def test_fleet_snapshot_counts_lifecycle_once_and_gates_warm_capacity_on_fresh_intake() -> None:
    now = datetime(2026, 9, 28, tzinfo=UTC)
    unit = PlatformReserveUnitRow(
        id="unit",
        workspace_id="platform",
        provider_ref="aws",
        preemptible=False,
        gpu_type="",
        gpu_count=0,
        cpu_millicores=8_000,
        memory_mib=16_384,
        reported_memory_mib=16_000,
        desired=3,
        stopped=1,
        retiring_stopped=0,
        retained=1,
        observed=5,
        provider_committed=5,
        phase=ComputeUnitPhase.Ready,
        degraded_reason=None,
        degraded_at=None,
        last_capacity_failure_at=None,
        registration_timeout_seconds=300,
        replacement_machine_id="",
        billing_minimum_seconds=None,
    )
    serving = PlatformReserveInstanceRow(
        unit_id=unit.id,
        status="active",
        instance_id="i-serving",
        machine_id="serving",
        availability_zone="zone",
        billing_started_at=None,
        missing=False,
        capacity_state=AgentCapacityState.Available,
        protected=False,
        containers=0,
        pinned=0,
        load_cpu_millicores=0,
        load_memory_mib=0,
        load_gpu_count=0,
        machine_lifecycle=MachineLifecycle.Ready,
    )
    rows = PlatformReserveRows(
        (unit,),
        (
            serving,
            replace(serving, instance_id="i-stale", machine_id="stale"),
            replace(
                serving,
                instance_id="i-starting",
                machine_id="starting",
                machine_lifecycle=MachineLifecycle.Joining,
            ),
            replace(
                serving,
                instance_id="i-sleep",
                machine_id="sleep",
                status="stopped",
                machine_lifecycle=MachineLifecycle.Stopped,
                sleep_attempt_id="attempt",
                sleep_requested_mode=CapacitySleepMode.Hibernate,
                sleep_accepted_mode=CapacitySleepMode.Hibernate,
                image_evidence=CapacityImageEvidence.Saved,
            ),
            replace(
                serving,
                instance_id="i-cleanup",
                machine_id="cleanup",
                status="deleted",
                machine_lifecycle=MachineLifecycle.Deleted,
            ),
        ),
    )
    snapshot = fleet_reserve_snapshot(
        rows,
        purchasable_providers=frozenset({"aws"}),
        now=now,
        ready_machine_ids=frozenset({"serving"}),
        release=None,
    )
    assert snapshot.committed_cpu_machines == 5
    assert {machine.key: machine.state for machine in snapshot.machines} == {
        "serving": ReserveMachineState.Serving,
        "stale": ReserveMachineState.Unavailable,
        "starting": ReserveMachineState.Starting,
        "sleep": ReserveMachineState.ImageSaved,
        "cleanup": ReserveMachineState.Terminating,
    }
    plan = plan_market_reserve(FleetCapacityPolicy(), snapshot, ReserveConditions(now=now))
    market = plan.market(ReserveMarket(False))
    assert market is not None
    capacity = snapshot.units[0].machine
    assert market.warm_free == capacity
    assert market.warm_pending == capacity
    assert sum(item.machines for item in market.observed.values()) == 5
