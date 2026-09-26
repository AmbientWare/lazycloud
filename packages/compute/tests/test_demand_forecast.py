from datetime import UTC, datetime, timedelta

from compute.demand_forecast import DemandSample, forecast_demand
from compute.fleet_resources import Capacity


def test_forecast_only_uses_recent_observed_arrivals_and_preserves_pending() -> None:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    pending = Capacity(2_000, 4_096, 1)
    excluded = Capacity(100_000, 1_000_000, 100)
    forecast = forecast_demand(
        [
            DemandSample(now + timedelta(microseconds=1), excluded),
            DemandSample(now - timedelta(seconds=600), excluded),
        ],
        now=now,
        resume_seconds=10,
        provision_seconds=180,
        pending=pending,
    )

    assert forecast.warm == pending
    assert forecast.total == pending
    assert forecast.sample_count == 0
    assert forecast.request_shapes == ()


def test_cumulative_horizon_counts_pending_and_burst_once() -> None:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    request = Capacity(1_000, 2_048, 1)
    pending = Capacity(500, 1_024)
    forecast = forecast_demand(
        [DemandSample(now - timedelta(seconds=1), request, count=2)],
        now=now,
        resume_seconds=60,
        provision_seconds=120,
        pending=pending,
    )

    assert forecast.warm == pending + request * 3
    assert forecast.total == pending + request * 5
    assert forecast.total.covers(forecast.warm)
    assert forecast.burst == request
    assert forecast.sample_count == 2


def test_bounded_shapes_preserve_fit_and_round_up_fractional_demand() -> None:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    shapes = [Capacity(index, 100 - index, 1) for index in range(1, 65)]
    forecast = forecast_demand(
        [DemandSample(now, shape) for shape in shapes],
        now=now,
        resume_seconds=0.01,
        provision_seconds=0.01,
    )

    assert len(forecast.request_shapes) <= 32
    assert all(any(kept.covers(shape) for kept in forecast.request_shapes) for shape in shapes)
    assert forecast.warm == Capacity(65, 100, 2)
    assert forecast.total == forecast.warm


def test_scheduled_capacity_uses_its_horizon_without_becoming_arrival_rate() -> None:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    request = Capacity(2_000, 4_096)
    scheduled = [
        DemandSample(now + timedelta(seconds=10), request, count=2),
        DemandSample(now + timedelta(seconds=60), request),
        DemandSample(now + timedelta(seconds=61), request * 100),
    ]
    forecast = forecast_demand(
        [],
        now=now,
        resume_seconds=10,
        provision_seconds=60,
        scheduled=scheduled,
    )

    assert forecast.warm == request * 2
    assert forecast.total == request * 3
    assert forecast.sample_count == 0
    assert forecast.burst.empty
    assert forecast.short_arrivals.empty
    assert forecast.scheduled_total == request * 3
    assert forecast.largest_request == request
