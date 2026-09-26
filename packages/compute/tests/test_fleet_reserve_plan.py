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
from compute.fleet_resources import Capacity, ReserveMarket, ReserveOffer

NOW = datetime(2026, 9, 24, 12, tzinfo=UTC)
MARKET = ReserveMarket(preemptible=False)
SMALL = ReserveOffer("small", MARKET, Capacity(8_000, 16_384), 8_000, 100_000, 10_000, True)
LARGE = ReserveOffer("large", MARKET, Capacity(32_000, 65_536), 32_000, 300_000, 20_000, True)


def _policy(target: Capacity) -> FleetCapacityPolicy:
    return FleetCapacityPolicy(
        spot=MarketReserve(),
        on_demand=MarketReserve(warm=HeadroomTarget(floor=target, maximum=target)),
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
                        ReserveMachineState.Reserve,
                        stopped_resumable=True,
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


def test_growth_respects_shared_machine_and_running_cpu_limits() -> None:
    policy = _policy(LARGE.machine * 4).model_copy(
        update={"max_cpu_instances": 2, "max_running_cpu_millicores": 16_000}
    )
    market = plan_market_reserve(policy, _snapshot(), ReserveConditions(now=NOW)).market(MARKET)
    assert market is not None
    purchased = {SMALL.key: SMALL, LARGE.key: LARGE}
    assert sum(action.count for action in market.growth) <= 2
    assert (
        sum(
            purchased[action.offer_key].nominal_cpu_millicores * action.count
            for action in market.growth
        )
        <= 16_000
    )
    assert not market.shortfall.empty


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
        machine_limit=3,
        running_cpu_millicores=32_000,
    )
    assert not plan.remaining
    assert len(plan.nodes) == 1
    assert plan.nodes[0].offer_key == medium.key
    assert set(plan.nodes[0].request_indices) == {0, 1, 2}


def test_partial_request_purchase_preserves_limits_and_reports_unplaced_work() -> None:
    request = Capacity(5_000, 10_240)
    plan = plan_request_capacity(
        (SMALL,),
        (request,) * 3,
        machine_limit=3,
        running_cpu_millicores=16_000,
    )
    assert len(plan.nodes) == 2
    assert plan.remaining == (request,)
    placed = [index for node in plan.nodes for index in node.request_indices]
    assert len(placed) == len(set(placed)) == 2
