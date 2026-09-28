from dataclasses import replace
from datetime import UTC, datetime

from compute.activation_timing import reserve_forecast
from compute.fleet_policy import (
    FleetCapacityPolicy,
    FleetReserveSnapshot,
    ReserveMachine,
    ReserveUnit,
)
from compute.fleet_resources import Capacity, ReserveMarket, ReservePlacement
from database.repositories.capacity_activations import CapacityActivationSummary
from shared.capacity_lifecycle import CapacityActivationKind, CapacityRestoreOutcome
from shared.fleet_capacity import ReserveMachineState

MARKET = ReserveMarket(False)
NOW = datetime(2026, 9, 28, tzinfo=UTC)
UNIT = ReserveUnit(
    "small",
    MARKET,
    Capacity(8_000, 16_384),
    8_000,
    0,
    1,
    True,
    placement=ReservePlacement(region="us-east", architecture="x86_64"),
    provider="aws",
    provider_region="us-east-1",
    instance_type="c6a.2xlarge",
)
RESTORE = CapacityActivationSummary(
    provider="aws",
    region="us-east-1",
    architecture="x86_64",
    instance_type="c6a.2xlarge",
    gpu_type="",
    kind=CapacityActivationKind.Resume,
    restore_outcome=CapacityRestoreOutcome.MemoryRestored,
    attempts=30,
    ready=30,
    prepared=0,
    failed=0,
    p95_ready_seconds=10,
    p95_provider_seconds=5,
)


def test_fragmented_reserves_cannot_shorten_large_request_forecast() -> None:
    snapshot = FleetReserveSnapshot(
        (replace(UNIT, stopped=8),),
        tuple(
            ReserveMachine(str(index), UNIT.unit_id, ReserveMachineState.ImageSaved)
            for index in range(8)
        ),
        8,
        0,
        0,
    )
    request = UNIT.machine * 2
    forecast = reserve_forecast(
        FleetCapacityPolicy(),
        snapshot,
        (RESTORE,),
        market=MARKET,
        history=(),
        scheduled=(),
        pending=request,
        pending_shapes=(request,),
        now=NOW,
    )
    assert forecast.warm_horizon_seconds == 360
    assert forecast.timing is not None
    assert forecast.timing.kind is CapacityActivationKind.Provision


def test_resume_estimates_include_cold_fallbacks_from_matching_hardware() -> None:
    snapshot = FleetReserveSnapshot(
        (UNIT,),
        (ReserveMachine("node", UNIT.unit_id, ReserveMachineState.ImageSaved),),
        1,
        0,
        0,
    )
    cold = replace(
        RESTORE,
        restore_outcome=CapacityRestoreOutcome.ColdBoot,
        attempts=1,
        ready=1,
        p95_ready_seconds=145,
    )
    forecast = reserve_forecast(
        FleetCapacityPolicy(),
        snapshot,
        (RESTORE, cold),
        market=MARKET,
        history=(),
        scheduled=(),
        pending=UNIT.machine,
        pending_shapes=(UNIT.machine,),
        now=NOW,
    )
    assert forecast.warm_horizon_seconds == 205
    assert forecast.timing is not None
    assert forecast.timing.cold_boot_samples == 1
    other_hardware = reserve_forecast(
        FleetCapacityPolicy(),
        snapshot,
        (RESTORE, replace(cold, instance_type="m6a.8xlarge")),
        market=MARKET,
        history=(),
        scheduled=(),
        pending=UNIT.machine,
        pending_shapes=(UNIT.machine,),
        now=NOW,
    )
    assert other_hardware.warm_horizon_seconds == 70


def test_held_preparation_does_not_establish_serving_ready_timing() -> None:
    snapshot = FleetReserveSnapshot(
        (UNIT,),
        (ReserveMachine("node", UNIT.unit_id, ReserveMachineState.ImageSaved),),
        1,
        0,
        0,
    )
    forecast = reserve_forecast(
        FleetCapacityPolicy(),
        snapshot,
        (replace(RESTORE, ready=0, prepared=30, p95_ready_seconds=None),),
        market=MARKET,
        history=(),
        scheduled=(),
        pending=UNIT.machine,
        now=NOW,
    )
    assert forecast.warm_horizon_seconds == 90
    assert forecast.timing is not None and forecast.timing.fallback_used
