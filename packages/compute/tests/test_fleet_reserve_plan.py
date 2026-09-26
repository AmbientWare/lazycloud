from dataclasses import replace
from datetime import UTC, datetime, timedelta

from compute.capacity_acquisition import plan_request_capacity
from compute.fleet_policy import (
    FleetCapacityPolicy,
    FleetReserveSnapshot,
    GrowthKind,
    HeadroomTarget,
    MarketReserve,
    ReserveConditions,
    ReserveMachine,
    ReserveMachineState,
    ReserveUnit,
    plan_market_reserve,
)
from compute.fleet_resources import (
    Capacity,
    ReserveDemand,
    ReserveMarket,
    ReserveOffer,
    ReservePlacement,
)
from compute.maintenance_policy import MaintenanceBudget, MaintenanceCandidate, plan_maintenance

NOW = datetime(2026, 9, 24, 12, tzinfo=UTC)
MARKET = ReserveMarket(preemptible=False)
SMALL = ReserveOffer("small", MARKET, Capacity(8_000, 16_384), 8_000, 100_000, 10_000, True)
LARGE = ReserveOffer("large", MARKET, Capacity(32_000, 65_536), 32_000, 300_000, 20_000, True)


def _policy(target: Capacity) -> FleetCapacityPolicy:
    return FleetCapacityPolicy(
        spot=MarketReserve(),
        on_demand=MarketReserve(warm=HeadroomTarget(floor=target)),
        gpu={},
    )


def _snapshot(
    units: tuple[ReserveUnit, ...] = (),
    machines: tuple[ReserveMachine, ...] = (),
) -> FleetReserveSnapshot:
    return FleetReserveSnapshot(
        units=units,
        machines=machines,
        committed_cpu_machines=sum(unit.desired + unit.stopped for unit in units),
        committed_gpu_machines=0,
        running_cpu_millicores=sum(unit.desired * unit.nominal_cpu_millicores for unit in units),
        offers=(SMALL, LARGE),
    )


def _unit(offer: ReserveOffer, *, desired: int = 0, stopped: int = 0) -> ReserveUnit:
    return ReserveUnit(
        unit_id=offer.key,
        market=offer.market,
        machine=offer.machine,
        nominal_cpu_millicores=offer.nominal_cpu_millicores,
        desired=desired,
        stopped=stopped,
        growable=True,
        hourly_cost_micros=offer.hourly_cost_micros,
        stopped_hourly_cost_micros=offer.stopped_hourly_cost_micros,
    )


def test_purchases_choose_lower_total_cost_for_required_capacity() -> None:
    for target, expected in ((SMALL.machine, SMALL), (SMALL.machine * 4, LARGE)):
        market = plan_market_reserve(
            _policy(target), _snapshot(), ReserveConditions(now=NOW)
        ).market(MARKET)
        assert market is not None
        purchases = [action for action in market.growth if action.kind is GrowthKind.Buy]
        assert len(purchases) == 1
        assert purchases[0].offer_key == expected.key
        assert purchases[0].count == 1
        assert market.shortfall.empty


def test_large_request_must_fit_one_host_despite_aggregate_capacity() -> None:
    machines = tuple(
        ReserveMachine(str(index), SMALL.key, ReserveMachineState.Serving) for index in range(4)
    )
    market = plan_market_reserve(
        _policy(SMALL.machine),
        _snapshot((_unit(SMALL, desired=4),), machines),
        ReserveConditions(now=NOW, request_shapes={MARKET: (Capacity(16_000, 32_768),)}),
    ).market(MARKET)
    assert market is not None
    assert any(action.offer_key == LARGE.key for action in market.growth)


def test_only_ready_stopped_capacity_can_replace_a_purchase() -> None:
    for ready in (True, False):
        market = plan_market_reserve(
            _policy(SMALL.machine),
            _snapshot(
                (_unit(SMALL, stopped=1),),
                (
                    ReserveMachine(
                        "reserve",
                        SMALL.key,
                        ReserveMachineState.Stopped,
                        ready=ready,
                    ),
                ),
            ),
            ReserveConditions(now=NOW),
        ).market(MARKET)
        assert market is not None
        assert any(action.kind is GrowthKind.Resume for action in market.growth) == ready
        assert any(action.kind is GrowthKind.Buy for action in market.growth) != ready


def test_retirement_preserves_the_only_node_that_fits_recent_requests() -> None:
    market = plan_market_reserve(
        _policy(SMALL.machine * 4),
        _snapshot(
            (_unit(SMALL, desired=4), _unit(LARGE, desired=1)),
            (
                *(
                    ReserveMachine(str(index), SMALL.key, ReserveMachineState.Serving)
                    for index in range(4)
                ),
                ReserveMachine("large", LARGE.key, ReserveMachineState.Serving),
            ),
        ),
        ReserveConditions(now=NOW, request_shapes={MARKET: (LARGE.machine,)}),
    ).market(MARKET)
    assert market is not None
    assert market.retained[LARGE.key] == 1


def test_unavailable_request_shape_is_reported_despite_aggregate_headroom() -> None:
    market = plan_market_reserve(
        _policy(SMALL.machine),
        _snapshot(
            (_unit(SMALL, desired=1),),
            (ReserveMachine("small", SMALL.key, ReserveMachineState.Serving),),
        ),
        ReserveConditions(now=NOW, request_shapes={MARKET: (LARGE.machine * 2,)}),
    ).market(MARKET)
    assert market is not None
    assert market.unmet_shapes == (LARGE.machine * 2,)
    assert market.reason


def test_existing_large_fleet_does_not_block_demand_growth() -> None:
    snapshot = replace(_snapshot(), committed_cpu_machines=1000, running_cpu_millicores=32_000_000)
    market = plan_market_reserve(
        _policy(LARGE.machine * 4), snapshot, ReserveConditions(now=NOW)
    ).market(MARKET)
    assert market is not None
    assert market.shortfall.empty
    assert sum(action.count for action in market.growth) == 4


def test_demand_and_recovery_keep_elective_growth_out_of_their_way() -> None:
    for conditions in (
        ReserveConditions(now=NOW, demand=frozenset({MARKET})),
        ReserveConditions(now=NOW, recovering=frozenset({MARKET})),
    ):
        market = plan_market_reserve(_policy(SMALL.machine), _snapshot(), conditions).market(MARKET)
        assert market is not None and not market.growth


def test_pending_capacity_does_not_justify_retiring_ready_capacity() -> None:
    market = plan_market_reserve(
        _policy(SMALL.machine),
        _snapshot(
            (_unit(SMALL, desired=1), _unit(LARGE, desired=1)),
            (
                ReserveMachine("ready", SMALL.key, ReserveMachineState.Serving),
                ReserveMachine("pending", LARGE.key, ReserveMachineState.Starting, ready=False),
            ),
        ),
        ReserveConditions(now=NOW),
    ).market(MARKET)
    assert market is not None
    assert market.retained[SMALL.key] == 1
    assert market.warm_free == SMALL.machine
    assert market.warm_pending == LARGE.machine


def test_consolidation_preserves_pinned_work_and_required_headroom() -> None:
    for pinned, target, expected in (
        (0, SMALL.machine, "busy"),
        (1, SMALL.machine, ""),
        (0, LARGE.machine * 2, ""),
    ):
        market = plan_market_reserve(
            _policy(target),
            _snapshot(
                (_unit(LARGE, desired=2),),
                (
                    ReserveMachine(
                        "busy",
                        LARGE.key,
                        ReserveMachineState.Serving,
                        load=Capacity(1_000, 2_048),
                        containers=1,
                        pinned=pinned,
                    ),
                    ReserveMachine("idle", LARGE.key, ReserveMachineState.Serving),
                ),
            ),
            ReserveConditions(now=NOW, lightly_used_since={"busy": NOW - timedelta(hours=1)}),
        ).market(MARKET)
        assert market is not None
        assert market.consolidate == expected


def test_request_purchase_cost_accounts_for_each_container_fitting_a_node() -> None:
    medium = ReserveOffer("medium", MARKET, Capacity(16_000, 32_768), 16_000, 250_000, 10_000, True)
    requests = [Capacity(5_000, 10_240)] * 3
    plan = plan_request_capacity(
        (SMALL, medium),
        requests,
    )
    assert not plan.remaining
    assert len(plan.nodes) == 1
    assert plan.nodes[0].offer_key == medium.key
    assert set(plan.nodes[0].request_indices) == {0, 1, 2}


def test_partial_request_purchase_reports_unplaceable_shapes() -> None:
    request = Capacity(5_000, 10_240)
    plan = plan_request_capacity(
        (SMALL,),
        (request, request, LARGE.machine),
    )
    assert len(plan.nodes) == 2
    assert plan.remaining == (LARGE.machine,)
    placed = [index for node in plan.nodes for index in node.request_indices]
    assert len(placed) == len(set(placed)) == 2


def test_preparing_reserve_cannot_replace_ready_reserve_on_retirement() -> None:
    cheap = replace(SMALL, key="preparing", stopped_hourly_cost_micros=1)
    policy = FleetCapacityPolicy(
        spot=MarketReserve(),
        on_demand=MarketReserve(stopped=HeadroomTarget(floor=SMALL.machine)),
        gpu={},
    )
    market = plan_market_reserve(
        policy,
        _snapshot(
            (_unit(SMALL, stopped=1), _unit(cheap, stopped=1)),
            (
                ReserveMachine("ready", SMALL.key, ReserveMachineState.Stopped),
                ReserveMachine("preparing", cheap.key, ReserveMachineState.Preparing, ready=False),
            ),
        ),
        ReserveConditions(now=NOW),
    ).market(MARKET)
    assert market is not None
    assert market.stopped[SMALL.key] == 1
    assert market.stopped[cheap.key] == 0
    assert market.stopped_ready == SMALL.machine


def test_resumed_machine_cannot_cover_retirement_of_the_remaining_ready_reserve() -> None:
    cheap = replace(SMALL, key="cheap", hourly_cost_micros=10, stopped_hourly_cost_micros=1)
    expensive = replace(
        SMALL, key="expensive", hourly_cost_micros=20, stopped_hourly_cost_micros=10
    )
    policy = FleetCapacityPolicy(
        spot=MarketReserve(),
        on_demand=MarketReserve(
            warm=HeadroomTarget(floor=SMALL.machine), stopped=HeadroomTarget(floor=SMALL.machine)
        ),
        gpu={},
    )
    market = plan_market_reserve(
        policy,
        _snapshot(
            (_unit(cheap, stopped=2), _unit(expensive, stopped=1)),
            (
                ReserveMachine("resume", cheap.key, ReserveMachineState.Stopped),
                ReserveMachine("preparing", cheap.key, ReserveMachineState.Preparing, ready=False),
                ReserveMachine("keep", expensive.key, ReserveMachineState.Stopped),
            ),
        ),
        ReserveConditions(now=NOW),
    ).market(MARKET)
    assert market is not None
    assert any(
        action.kind is GrowthKind.Resume and action.unit_id == cheap.key for action in market.growth
    )
    assert market.stopped[expensive.key] == 1
    assert market.stopped_ready == SMALL.machine


def test_stopped_reserve_cannot_displace_verified_hibernated_coverage() -> None:
    stopped = replace(SMALL, key="stopped", stopped_hourly_cost_micros=1)
    policy = FleetCapacityPolicy(
        spot=MarketReserve(),
        on_demand=MarketReserve(stopped=HeadroomTarget(floor=SMALL.machine)),
        gpu={},
    )
    market = plan_market_reserve(
        policy,
        _snapshot(
            (_unit(SMALL, stopped=1), _unit(stopped, stopped=1)),
            (
                ReserveMachine("hibernated", SMALL.key, ReserveMachineState.Hibernated),
                ReserveMachine("stopped", stopped.key, ReserveMachineState.Stopped),
            ),
        ),
        ReserveConditions(now=NOW, hibernated_target={MARKET: SMALL.machine}),
    ).market(MARKET)
    assert market is not None
    assert market.stopped[SMALL.key] == 1
    assert market.stopped[stopped.key] == 0
    assert market.hibernated_capacity == SMALL.machine
    assert market.hibernated_shortfall.empty


def test_requested_hibernation_is_usable_without_claiming_verified_sleep() -> None:
    snapshot = _snapshot(
        (_unit(SMALL, stopped=1),),
        (ReserveMachine("requested", SMALL.key, ReserveMachineState.HibernationRequested),),
    )
    policy = FleetCapacityPolicy(
        spot=MarketReserve(),
        on_demand=MarketReserve(stopped=HeadroomTarget(floor=SMALL.machine)),
        gpu={},
    )
    retained = plan_market_reserve(policy, snapshot, ReserveConditions(now=NOW)).market(MARKET)
    assert retained is not None
    assert retained.stopped_ready == SMALL.machine
    assert retained.hibernation_requested_capacity == SMALL.machine
    assert retained.hibernated_capacity.empty
    assert retained.stopped_pending.empty
    activated = plan_market_reserve(
        _policy(SMALL.machine), snapshot, ReserveConditions(now=NOW)
    ).market(MARKET)
    assert activated is not None
    assert activated.growth[0].kind is GrowthKind.Resume
    assert activated.hibernation_requested_capacity.empty


def test_hibernation_target_prepares_once_and_waits_for_usable_replacement() -> None:
    fast = replace(SMALL, key="fast", supports_hibernation=True, stopped_hourly_cost_micros=20_000)
    policy = FleetCapacityPolicy(
        spot=MarketReserve(),
        on_demand=MarketReserve(stopped=HeadroomTarget(floor=SMALL.machine)),
        gpu={},
    )
    old = ReserveMachine("old", SMALL.key, ReserveMachineState.Stopped)
    initial = replace(_snapshot((_unit(SMALL, stopped=1),), (old,)), offers=(fast,))
    plan = plan_market_reserve(policy, initial, ReserveConditions(now=NOW)).market(MARKET)
    assert plan is not None
    assert plan.hibernated_target == SMALL.machine
    assert plan.stopped[SMALL.key] == 1
    assert plan.growth[0].kind is GrowthKind.Prepare
    assert plan.growth[0].offer_key == fast.key

    committed = replace(
        initial, units=(*initial.units, replace(_unit(fast, stopped=1), supports_hibernation=True))
    )
    for members in (
        (old,),
        (old, ReserveMachine("replacement", fast.key, ReserveMachineState.Preparing, ready=False)),
    ):
        plan = plan_market_reserve(
            policy, replace(committed, machines=members), ReserveConditions(now=NOW)
        ).market(MARKET)
        assert plan is not None
        assert not plan.growth
        assert plan.stopped == {SMALL.key: 1, fast.key: 1}

    requested = ReserveMachine("replacement", fast.key, ReserveMachineState.HibernationRequested)
    plan = plan_market_reserve(
        policy, replace(committed, machines=(old, requested)), ReserveConditions(now=NOW)
    ).market(MARKET)
    assert plan is not None
    assert not plan.growth
    assert plan.stopped == {SMALL.key: 0, fast.key: 1}
    assert plan.hibernation_requested_capacity == SMALL.machine
    assert plan.hibernated_capacity.empty

    fallback = replace(
        committed,
        units=(committed.units[1],),
        machines=(replace(requested, state=ReserveMachineState.Stopped),),
    )
    plan = plan_market_reserve(policy, fallback, ReserveConditions(now=NOW)).market(MARKET)
    assert plan is not None
    assert not plan.growth
    assert plan.stopped_ready == SMALL.machine
    assert plan.hibernation_requested_capacity.empty
    assert plan.hibernated_capacity.empty


def test_consolidation_keeps_its_destination_pool() -> None:
    cheaper = replace(LARGE, key="idle", hourly_cost_micros=200_000)
    market = plan_market_reserve(
        _policy(SMALL.machine),
        _snapshot(
            (_unit(LARGE, desired=1), _unit(cheaper, desired=1)),
            (
                ReserveMachine(
                    "busy",
                    LARGE.key,
                    ReserveMachineState.Serving,
                    load=Capacity(1_000, 2_048),
                    containers=1,
                ),
                ReserveMachine("destination", cheaper.key, ReserveMachineState.Serving),
            ),
        ),
        ReserveConditions(now=NOW, lightly_used_since={"busy": NOW - timedelta(hours=1)}),
    ).market(MARKET)
    assert market is not None
    assert market.consolidate == "busy"
    assert market.consolidation_destinations == ("destination",)
    assert market.retained[cheaper.key] == 1


def test_rightsize_keeps_source_and_waits_for_pending_replacement() -> None:
    source = ReserveMachine("large", LARGE.key, ReserveMachineState.Serving)
    conditions = ReserveConditions(now=NOW, lightly_used_since={"large": NOW - timedelta(hours=1)})
    snapshot = _snapshot((_unit(LARGE, desired=1),), (source,))
    market = plan_market_reserve(_policy(SMALL.machine), snapshot, conditions).market(MARKET)
    assert market is not None
    assert market.rightsize_source == "large"
    assert market.retained[LARGE.key] == 1
    assert market.growth[0].offer_key == SMALL.key
    pending = replace(snapshot, units=(*snapshot.units, _unit(SMALL, desired=1)))
    replanning = plan_market_reserve(_policy(SMALL.machine), pending, conditions).market(MARKET)
    assert replanning is not None
    assert not replanning.growth
    assert replanning.retained[LARGE.key] == 1


def test_forecast_requires_compatible_location_capacity() -> None:
    east = ReservePlacement(region="east", runtime="container")
    west = ReservePlacement(region="west", runtime="container")
    local = replace(SMALL, key="west", placement=west)
    remote = replace(SMALL, key="east", placement=east)
    snapshot = replace(
        _snapshot(
            (replace(_unit(local, desired=1), placement=west),),
            (ReserveMachine("west", local.key, ReserveMachineState.Serving),),
        ),
        offers=(local, remote),
    )
    market = plan_market_reserve(
        _policy(SMALL.machine),
        snapshot,
        ReserveConditions(
            now=NOW, placement_demands={MARKET: (ReserveDemand(SMALL.machine, east),)}
        ),
    ).market(MARKET)
    assert market is not None
    assert any(action.offer_key == "east" for action in market.growth)
    assert not market.unmet_placements
    pending = replace(
        snapshot, units=(*snapshot.units, replace(_unit(remote, desired=1), placement=east))
    )
    replanned = plan_market_reserve(
        _policy(SMALL.machine),
        pending,
        ReserveConditions(
            now=NOW, placement_demands={MARKET: (ReserveDemand(SMALL.machine, east),)}
        ),
    ).market(MARKET)
    assert replanned is not None
    assert not replanned.growth
    assert not replanned.unmet_placements


def test_location_forecast_does_not_resume_an_unusable_reserve() -> None:
    east = ReservePlacement(region="east")
    west = ReservePlacement(region="west")
    offer = replace(SMALL, placement=east)
    snapshot = replace(
        _snapshot(
            (replace(_unit(SMALL, stopped=1), placement=west),),
            (ReserveMachine("west", SMALL.key, ReserveMachineState.Stopped),),
        ),
        offers=(offer,),
    )
    market = plan_market_reserve(
        _policy(SMALL.machine),
        snapshot,
        ReserveConditions(
            now=NOW, placement_demands={MARKET: (ReserveDemand(SMALL.machine, east),)}
        ),
    ).market(MARKET)
    assert market is not None
    assert not any(action.kind is GrowthKind.Resume for action in market.growth)
    assert sum(action.count for action in market.growth if action.kind is GrowthKind.Buy) == 1
    assert not market.unmet_placements


def test_maintenance_concurrency_follows_ready_resources_and_exclusive_ownership() -> None:
    candidates = [
        MaintenanceCandidate(
            str(index),
            MARKET.key,
            unavailable=SMALL.machine,
            replacement_machine_id=f"replacement-{index // 2}",
        )
        for index in range(1000)
    ]
    selected = plan_maintenance(
        candidates,
        MaintenanceBudget(
            ready={MARKET.key: SMALL.machine * 1000},
            required_ready={MARKET.key: SMALL.machine * 300},
        ),
    )
    assert len(selected) == 500
    assert len({item.replacement_machine_id for item in selected}) == 500
    selected = plan_maintenance(
        candidates,
        MaintenanceBudget(
            ready={MARKET.key: SMALL.machine * 1000},
            required_ready={MARKET.key: SMALL.machine * 950},
        ),
    )
    assert len(selected) == 50
