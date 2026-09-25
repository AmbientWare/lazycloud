from __future__ import annotations

from datetime import UTC, datetime, timedelta

from compute.fleet_policy import (
    Capacity,
    FleetCapacityPolicy,
    FleetReservePlan,
    FleetReserveSnapshot,
    GrowthKind,
    MachineRole,
    ReserveConditions,
    ReserveMachine,
    ReserveMachineState,
    ReserveMarket,
    ReserveUnit,
    plan_market_reserve,
)
from shared.container_requests import schedulable_capacity

NOW = datetime(2026, 9, 24, 12, tzinfo=UTC)
SPOT = ReserveMarket(preemptible=True)
ON_DEMAND = ReserveMarket(preemptible=False)
POLICY = FleetCapacityPolicy(gpu={})


def _unit(
    unit_id: str,
    *,
    market: ReserveMarket = SPOT,
    cpu: int = 8_000,
    memory: int = 16 * 1024,
    desired: int = 0,
    stopped: int = 0,
    growable: bool = True,
) -> ReserveUnit:
    return ReserveUnit(
        unit_id=unit_id,
        market=market,
        role=POLICY.role(cpu_millicores=cpu, memory_mib=memory, gpu_count=0),
        machine=Capacity(schedulable_capacity(cpu), schedulable_capacity(memory)),
        nominal_cpu_millicores=cpu,
        desired=desired,
        stopped=stopped,
        growable=growable,
    )


def _serving(
    key: str, unit: str, *, load: Capacity = Capacity(), pinned: int = 0
) -> ReserveMachine:
    return ReserveMachine(
        key=key,
        unit_id=unit,
        state=ReserveMachineState.Serving,
        load=load,
        containers=0 if load.empty else 1,
        pinned=pinned,
    )


def _stopped(key: str, unit: str) -> ReserveMachine:
    return ReserveMachine(
        key=key, unit_id=unit, state=ReserveMachineState.Reserve, stopped_resumable=True
    )


def _plan(
    units: list[ReserveUnit],
    machines: list[ReserveMachine],
    *,
    policy: FleetCapacityPolicy = POLICY,
    lightly_used_since: dict[str, datetime] | None = None,
    demand: frozenset[ReserveMarket] = frozenset(),
    recovering: frozenset[ReserveMarket] = frozenset(),
    consolidating: frozenset[ReserveMarket] = frozenset(),
) -> FleetReservePlan:
    snapshot = FleetReserveSnapshot(
        units=tuple(units),
        machines=tuple(machines),
        committed_cpu_machines=sum(unit.desired + unit.stopped for unit in units),
        committed_gpu_machines=0,
        running_cpu_millicores=sum(unit.desired * unit.nominal_cpu_millicores for unit in units),
    )
    return plan_market_reserve(
        policy,
        snapshot,
        ReserveConditions(
            now=NOW,
            lightly_used_since=lightly_used_since or {},
            demand=demand,
            recovering=recovering,
            consolidating=consolidating,
        ),
    )


def test_a_quiet_market_grows_by_small_machines_and_resumes_before_it_buys() -> None:
    empty = _plan([], [])
    spot = empty.market(SPOT)
    assert spot is not None and spot.growth is not None
    assert (spot.growth.kind, spot.growth.role) == (GrowthKind.Buy, MachineRole.Small)

    with_reserve = _plan(
        [_unit("small", stopped=1), _unit("large", cpu=32_000, memory=128 * 1024, stopped=1)],
        [_stopped("s", "small"), _stopped("l", "large")],
    ).market(SPOT)
    assert with_reserve is not None and with_reserve.growth is not None
    assert with_reserve.growth.kind is GrowthKind.Resume
    assert with_reserve.growth.unit_id == "small"


def test_a_loaded_market_grows_by_large_machines() -> None:
    busy = Capacity(7_000, 14_000)
    units = [_unit("small", desired=3)]
    machines = [_serving(f"m{index}", "small", load=busy) for index in range(3)]
    spot = _plan(units, machines).market(SPOT)
    assert spot is not None and not spot.quiet and spot.growth is not None
    assert (spot.growth.kind, spot.growth.role) == (GrowthKind.Buy, MachineRole.Large)


def test_waiting_work_and_recovery_keep_reserve_growth_out_of_their_way() -> None:
    for spot in (
        _plan([], [], demand=frozenset({SPOT})).market(SPOT),
        _plan([], [], recovering=frozenset({SPOT})).market(SPOT),
    ):
        assert spot is not None and spot.growth is None


def test_each_market_takes_one_growth_action_a_pass() -> None:
    plan = _plan([], [])
    spot = plan.market(SPOT)
    assert spot is not None and spot.growth is not None
    # Spot needs warm headroom and a stopped reserve; the reserve waits a pass.
    assert spot.growth.kind is GrowthKind.Buy
    on_demand = plan.market(ON_DEMAND)
    assert on_demand is not None and on_demand.growth is not None
    assert (on_demand.growth.kind, on_demand.growth.role) == (GrowthKind.Prepare, MachineRole.Large)


def test_the_fleet_caps_stop_reserve_growth() -> None:
    capped = POLICY.model_copy(update={"max_cpu_instances": 0})
    for market in _plan([], [], policy=capped).markets:
        assert market.growth is None
    # A stopped reserve does not run, so only the running-vCPU cap's warm purchase stops.
    no_vcpu = POLICY.model_copy(update={"max_running_cpu_millicores": 4_000})
    spot = _plan([], [], policy=no_vcpu).market(SPOT)
    assert spot is not None and spot.growth is not None
    assert spot.growth.kind is GrowthKind.Prepare


def test_idle_machines_beyond_warm_headroom_are_released_largest_first_when_quiet() -> None:
    units = [_unit("small", desired=2), _unit("large", cpu=32_000, memory=128 * 1024, desired=1)]
    machines = [_serving("s1", "small"), _serving("s2", "small"), _serving("l1", "large")]
    spot = _plan(units, machines).market(SPOT)
    assert spot is not None and spot.warm_free.covers(spot.warm_target)
    assert spot.retained == {"small": 2, "large": 0}


def test_busy_machines_are_retained_and_a_shortfall_holds_every_idle_machine() -> None:
    units = [_unit("small", desired=2)]
    busy = _serving("busy", "small", load=Capacity(6_000, 12_000))
    spot = _plan(units, [busy, _serving("idle", "small")]).market(SPOT)
    assert spot is not None
    assert spot.retained == {"small": 2}


def test_stopped_reserves_are_prepared_to_the_target_and_retired_above_it() -> None:
    large = _unit("large", market=ON_DEMAND, cpu=32_000, memory=128 * 1024, stopped=1)
    after_large = _plan([large], [_stopped("l", "large")]).market(ON_DEMAND)
    assert after_large is not None and after_large.growth is not None
    assert (after_large.growth.kind, after_large.growth.role) == (
        GrowthKind.Prepare,
        MachineRole.Small,
    )

    surplus = _unit("large", market=ON_DEMAND, cpu=32_000, memory=128 * 1024, stopped=3)
    small = _unit("small", market=ON_DEMAND, stopped=1)
    kept = _plan([surplus, small], []).market(ON_DEMAND)
    assert kept is not None and kept.growth is None
    assert kept.stopped == {"large": 1, "small": 1}


def test_the_spot_stopped_target_covers_the_busiest_spot_machine() -> None:
    heavy = Capacity(40_000, 200 * 1024)
    units = [_unit("big", cpu=64_000, memory=256 * 1024, desired=1)]
    spot = _plan(units, [_serving("m", "big", load=heavy)]).market(SPOT)
    assert spot is not None and spot.stopped_target.covers(heavy)


def _consolidating_fleet(*, pinned: int = 0) -> tuple[list[ReserveUnit], list[ReserveMachine]]:
    units = [_unit("large", cpu=32_000, memory=128 * 1024, desired=3)]
    light = Capacity(2_000, 4_000)
    return units, [
        _serving("a", "large", load=Capacity(10_000, 20_000)),
        _serving("b", "large", load=light, pinned=pinned),
        _serving("c", "large"),
    ]


def test_consolidation_waits_out_its_window_and_never_picks_pinned_work() -> None:
    units, machines = _consolidating_fleet()
    watched = _plan(units, machines).market(SPOT)
    assert watched is not None
    assert (watched.consolidation_candidate, watched.consolidate) == ("b", "")

    since = {"b": NOW - timedelta(seconds=POLICY.consolidation_seconds)}
    ready = _plan(units, machines, lightly_used_since=since).market(SPOT)
    assert ready is not None and ready.consolidate == "b"

    busy_market = _plan(
        units, machines, lightly_used_since=since, consolidating=frozenset({SPOT})
    ).market(SPOT)
    assert busy_market is not None and busy_market.consolidate == ""

    pinned_units, pinned_machines = _consolidating_fleet(pinned=1)
    pinned = _plan(pinned_units, pinned_machines, lightly_used_since=since)
    spot = pinned.market(SPOT)
    assert spot is not None and spot.consolidation_candidate == ""
    assert "b" not in pinned.lightly_used_since


def test_consolidation_keeps_enough_headroom_for_the_work_it_moves() -> None:
    units = [_unit("small", desired=2)]
    machines = [
        _serving("a", "small", load=Capacity(1_000, 2_000)),
        _serving("b", "small", load=Capacity(1_000, 2_000)),
    ]
    since = {"a": NOW - timedelta(hours=1), "b": NOW - timedelta(hours=1)}
    spot = _plan(units, machines, lightly_used_since=since).market(SPOT)
    assert spot is not None and spot.consolidate == ""
