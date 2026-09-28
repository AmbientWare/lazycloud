from datetime import UTC, datetime, timedelta

from compute.fleet_policy import (
    FleetCapacityPolicy,
    FleetReservePlan,
    FleetReserveSnapshot,
    ReserveConditions,
    plan_market_reserve,
)
from compute.reserve_state import RedisFleetReserveState
from tests.real_redis import RealRedisActors


def test_replicas_share_complete_reserve_decision(real_redis_actors: RealRedisActors) -> None:
    writer = RedisFleetReserveState(real_redis_actors.client())
    reader = RedisFleetReserveState(real_redis_actors.client())
    now = datetime.now(UTC)
    plan = plan_market_reserve(
        FleetCapacityPolicy(),
        FleetReserveSnapshot((), (), 0, 0, 0),
        ReserveConditions(now=now),
    )

    writer.publish(plan, generated_at=now, release=None)

    published = reader.published()
    assert published is not None
    assert published.generated_at == now
    assert tuple(published.markets.values()) == plan.markets
    assert published.lightly_used_since == plan.lightly_used_since


def test_missing_or_expired_plan_is_not_a_zero_capacity_decision(
    real_redis_actors: RealRedisActors,
) -> None:
    redis = real_redis_actors.client()
    state = RedisFleetReserveState(redis)
    now = datetime.now(UTC)
    plan = FleetReservePlan(markets=(), lightly_used_since={"busy-machine": now})

    assert state.published() is None
    state.publish(plan, generated_at=now - timedelta(hours=1), release=None)
    assert state.published() is None

    state.publish(plan, generated_at=now, release=None)
    published = state.published()
    assert published is not None
    assert published.markets == {}
    assert published.lightly_used_since == {"busy-machine": now}

    redis.expire(redis.key("compute", "fleet-reserve", "published"), 0)
    assert state.published() is None
