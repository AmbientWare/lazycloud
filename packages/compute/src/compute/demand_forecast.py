"""Container arrivals expected before reserves resume and new nodes provision."""

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta
from math import ceil, isfinite

from compute.fleet_resources import Capacity

SHORT_WINDOW_SECONDS = 60
HISTORY_SECONDS = 600
_MAX_REQUEST_SHAPES = 32


@dataclass(frozen=True, slots=True)
class DemandSample:
    observed_at: datetime
    capacity: Capacity
    count: int = 1
    duration_seconds: float | None = None

    def __post_init__(self) -> None:
        if self.count < 1:
            raise ValueError("container demand count must be positive")
        if self.duration_seconds is not None and (
            not isfinite(self.duration_seconds) or self.duration_seconds <= 0
        ):
            raise ValueError("observed duration must be positive and finite")


@dataclass(frozen=True, slots=True)
class DemandForecast:
    warm: Capacity
    total: Capacity
    largest_request: Capacity
    request_shapes: tuple[Capacity, ...]
    sample_count: int
    short_arrivals: Capacity
    long_arrivals: Capacity
    burst: Capacity
    scheduled_warm: Capacity
    scheduled_total: Capacity


def forecast_demand(
    samples: Iterable[DemandSample],
    *,
    now: datetime,
    resume_seconds: float,
    provision_seconds: float,
    pending: Capacity = Capacity(),
    scheduled: Iterable[DemandSample] = (),
) -> DemandForecast:
    """Forecast one market's additional capacity over two cumulative horizons.

    Pending demand appears once in each horizon. Completed allocations and frees
    belong to the fleet snapshot; arrivals do not imply that capacity is free.
    Policy floors apply outside this function, including when history is empty.
    """
    if now.utcoffset() is None:
        raise ValueError("demand forecasts require timezone-aware timestamps")
    if (
        not isfinite(resume_seconds)
        or not isfinite(provision_seconds)
        or resume_seconds < 0
        or provision_seconds < resume_seconds
    ):
        raise ValueError("provision horizon must cover a nonnegative resume horizon")
    _validate_capacity(pending)
    long_since = now - timedelta(seconds=HISTORY_SECONDS)
    short_since = now - timedelta(seconds=SHORT_WINDOW_SECONDS)
    short_arrivals = Capacity()
    long_arrivals = Capacity()
    largest = Capacity()
    shapes: set[Capacity] = set()
    sample_count = 0
    observed: list[DemandSample] = []
    for sample in samples:
        if sample.observed_at.utcoffset() is None:
            raise ValueError("demand forecasts require timezone-aware timestamps")
        if not long_since < sample.observed_at <= now:
            continue
        _validate_capacity(sample.capacity)
        long_arrivals += sample.capacity * sample.count
        if sample.observed_at > short_since:
            short_arrivals += sample.capacity * sample.count
        largest = largest.upper(sample.capacity)
        if not sample.capacity.empty:
            shapes.add(sample.capacity)
        sample_count += sample.count
        observed.append(sample)

    scheduled_warm = Capacity()
    scheduled_total = Capacity()
    scheduled_largest = Capacity()
    scheduled_events: list[tuple[float, Capacity]] = []
    for sample in scheduled:
        if sample.observed_at.utcoffset() is None:
            raise ValueError("demand forecasts require timezone-aware timestamps")
        until_arrival = (sample.observed_at - now).total_seconds()
        if not 0 < until_arrival <= provision_seconds:
            continue
        _validate_capacity(sample.capacity)
        capacity = sample.capacity * sample.count
        scheduled_events.append((until_arrival, capacity))
        if sample.duration_seconds is not None:
            scheduled_events.append(
                (until_arrival + sample.duration_seconds, Capacity() - capacity)
            )
        scheduled_largest = scheduled_largest.upper(sample.capacity)
        if not sample.capacity.empty:
            shapes.add(sample.capacity)

    scheduled_live = Capacity()
    for moment, delta in sorted(
        scheduled_events,
        key=lambda event: (
            event[0],
            event[1].cpu_millicores,
            event[1].memory_mib,
            event[1].gpu_count,
        ),
    ):
        scheduled_live += delta
        if moment <= provision_seconds:
            scheduled_total = scheduled_total.upper(scheduled_live)
        if moment <= resume_seconds:
            scheduled_warm = scheduled_warm.upper(scheduled_live)

    def horizon(seconds: float) -> Capacity:
        def occupancy(window: int) -> Capacity:
            cpu = memory = gpu = 0.0
            for sample in observed:
                if sample.observed_at <= now - timedelta(seconds=window):
                    continue
                occupied_seconds = (
                    min(seconds, sample.duration_seconds)
                    if sample.duration_seconds is not None
                    else seconds
                )
                factor = occupied_seconds * sample.count / window
                cpu += sample.capacity.cpu_millicores * factor
                memory += sample.capacity.memory_mib * factor
                gpu += sample.capacity.gpu_count * factor
            return Capacity(ceil(cpu), ceil(memory), ceil(gpu))

        short = occupancy(SHORT_WINDOW_SECONDS)
        long = occupancy(HISTORY_SECONDS)
        return pending + short.upper(long) + largest

    return DemandForecast(
        warm=horizon(resume_seconds) + scheduled_warm,
        total=horizon(provision_seconds) + scheduled_total,
        largest_request=largest.upper(scheduled_largest),
        request_shapes=_request_shapes(shapes),
        sample_count=sample_count,
        short_arrivals=short_arrivals,
        long_arrivals=long_arrivals,
        burst=largest,
        scheduled_warm=scheduled_warm,
        scheduled_total=scheduled_total,
    )


def _validate_capacity(capacity: Capacity) -> None:
    if min(capacity.cpu_millicores, capacity.memory_mib, capacity.gpu_count) < 0:
        raise ValueError("container demand cannot contain negative resources")


def _request_shapes(shapes: set[Capacity]) -> tuple[Capacity, ...]:
    ordered = sorted(
        shapes,
        key=lambda shape: (shape.gpu_count, shape.memory_mib, shape.cpu_millicores),
        reverse=True,
    )
    if len(ordered) <= _MAX_REQUEST_SHAPES:
        return tuple(ordered)
    overflow = Capacity()
    for shape in ordered[_MAX_REQUEST_SHAPES - 1 :]:
        overflow = overflow.upper(shape)
    # The merged shape may overstate a request, but never hides one that must fit.
    return (*ordered[: _MAX_REQUEST_SHAPES - 1], overflow)
