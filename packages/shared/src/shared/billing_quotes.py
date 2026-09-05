from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import ROUND_HALF_EVEN, Decimal
from typing import TypeAlias

from shared.enums import StringEnum
from shared.placement import AUTO_RATE_CLASS, PlacementRateClass
from shared.usage import UsageBillingOwner, UsageMetric

NANOS_PER_USD = 1_000_000_000
"""Money is an integer count of nanodollars.

The provider's meter carries the same integer, so a local total and an invoice
line compare exactly instead of through a second rounding step nobody owns.
"""

_MILLISECOND = timedelta(milliseconds=1)
_MICROSECOND = timedelta(microseconds=1)
_MICROSECONDS_PER_SECOND = Decimal(1_000_000)
_WHOLE_NANO = Decimal(1)
_MILLICORES_PER_CORE = Decimal(1_000)
_MIB_PER_GIB = Decimal(1_024)
BYTES_PER_GIB = Decimal(1_073_741_824)
"""Bytes in a gibibyte, which is the unit every byte-denominated price is quoted in."""


class BilledDimension(StringEnum):
    """What a quote prices, at the granularity the payment provider meters.

    Closed: a metric outside `BILLED_METRICS` produces no money at all, and there
    is no catch-all branch that could quietly start charging for one.
    """

    ComputeRuntime = "compute_runtime"
    NetworkEgress = "network_egress"
    VolumeStorage = "volume_storage"


class LedgerComponent(StringEnum):
    """What a quote prices, at the granularity the rate card publishes.

    A dimension is one provider meter and one invoice line; a component is one
    resource with its own quantity, its own unit and its own published rate.
    Compute has four of them because a container holds a processor, a gibibyte
    and a card independently, and one blended figure per millisecond cannot
    express a burst past the reservation at all.
    """

    ContainerTime = "container_time"
    Cpu = "cpu"
    Memory = "memory"
    Gpu = "gpu"
    Egress = "egress"
    VolumeStorage = "volume_storage"


class LedgerBasis(StringEnum):
    """Whether a segment's quantity is capacity held or capacity measured.

    On the ledger row rather than derived from the metric, because "was this
    window measured at all" is the question an operator asks of a charge, and a
    window whose measurement never arrived is billed at the floor and has to say
    so without a join back to what usage records exist.
    """

    Reserved = "reserved"
    Measured = "measured"


class UnpricedReason(StringEnum):
    """Why a metered span produced no money.

    A closed vocabulary rather than prose because it reaches a durable event an
    operator reads to decide what to publish, and prose there is a string nobody
    can filter on.
    """

    NoPublishedRate = "no_published_rate"
    NoRecordedPlacement = "no_recorded_placement"
    NoMeteringWindow = "no_metering_window"
    NoAccountOwner = "no_account_owner"


_COMPONENT_DIMENSIONS: Mapping[LedgerComponent, BilledDimension] = {
    LedgerComponent.ContainerTime: BilledDimension.ComputeRuntime,
    LedgerComponent.Cpu: BilledDimension.ComputeRuntime,
    LedgerComponent.Memory: BilledDimension.ComputeRuntime,
    LedgerComponent.Gpu: BilledDimension.ComputeRuntime,
    LedgerComponent.Egress: BilledDimension.NetworkEgress,
    LedgerComponent.VolumeStorage: BilledDimension.VolumeStorage,
}


@dataclass(frozen=True, slots=True)
class BilledUsage:
    """What one metric produces on the ledger.

    The basis belongs to the metric rather than to the component: a duration
    record states the capacity that was held over its window, and a CPU or memory
    record states what was measured over the same window. Both are needed, and
    which one a record is deciding is not something the pricer may infer from a
    quantity.
    """

    basis: LedgerBasis
    components: tuple[LedgerComponent, ...]

    @property
    def dimension(self) -> BilledDimension:
        return _COMPONENT_DIMENSIONS[self.components[0]]


BILLED_METRICS: Mapping[UsageMetric, BilledUsage] = {
    UsageMetric.ContainerDurationMilliseconds: BilledUsage(
        basis=LedgerBasis.Reserved,
        components=(
            LedgerComponent.ContainerTime,
            LedgerComponent.Cpu,
            LedgerComponent.Memory,
            LedgerComponent.Gpu,
        ),
    ),
    UsageMetric.CpuUsedCoreSeconds: BilledUsage(
        basis=LedgerBasis.Measured,
        components=(LedgerComponent.Cpu,),
    ),
    UsageMetric.MemoryRssByteSeconds: BilledUsage(
        basis=LedgerBasis.Measured,
        components=(LedgerComponent.Memory,),
    ),
    UsageMetric.NetworkEgressBytes: BilledUsage(
        basis=LedgerBasis.Measured,
        components=(LedgerComponent.Egress,),
    ),
    UsageMetric.PersistentVolumeByteSeconds: BilledUsage(
        basis=LedgerBasis.Measured,
        components=(LedgerComponent.VolumeStorage,),
    ),
}
"""Every metric that produces money, and what each one produces it for.

Every other metric is attribution or telemetry: it stays metered, it reaches the
dashboard, and it writes no ledger row.

A duration record carries the whole reservation — container time, processor,
memory and card — because that is the record whose existence is guaranteed: a
window that reaches the platform at all has one, and a container that never
reported its CPU still held everything nobody else could schedule onto. The
measured records add only what a window used above what it held, so the two
together are `max(reserved, measured)` without either one having to find the
other.
"""

# One record owes the provider one meter event, which needs one dimension, so a
# metric may not spread its components across two.
if any(
    {_COMPONENT_DIMENSIONS[component] for component in billed.components} != {billed.dimension}
    for billed in BILLED_METRICS.values()
):
    raise RuntimeError("every component one metric produces must belong to one billed dimension")


def _require_instant(value: datetime, field: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must name an instant, not a naive datetime")


def elapsed_seconds(started_at: datetime, ended_at: datetime) -> Decimal:
    """A window's length, exactly, as the unit every rate is published per.

    Through whole microseconds rather than `timedelta.total_seconds`, which is a
    float and would put a binary approximation underneath every reserved
    quantity on the ledger.
    """

    return Decimal((ended_at - started_at) // _MICROSECOND) / _MICROSECONDS_PER_SECOND


@dataclass(frozen=True, slots=True)
class ContainerShape:
    """What the control plane placed, which is what prices.

    Never what a worker says it placed: a worker token lives on a machine a
    customer has root on, so its labels decide attribution and never money.
    """

    billing_owner: UsageBillingOwner
    gpu_type: str
    cpu_millicores: int
    memory_mib: int
    gpu_count: int
    rate_class: PlacementRateClass = AUTO_RATE_CLASS


def reserved_quantity(
    component: LedgerComponent, shape: ContainerShape, seconds: Decimal
) -> Decimal:
    """Capacity held over `seconds`, in the component's quoted unit.

    Capacity held is capacity nobody else can schedule onto, so it is paid for
    whether or not it is used, and it is the floor a measured figure is compared
    against. Both conversions are exact: a thousandth and a 1024th each
    terminate in decimal.

    GPU is here and has no measured counterpart anywhere: a card is allocated
    whole, and holding one idle costs the platform exactly what using it does.
    """

    if component is LedgerComponent.ContainerTime:
        return seconds
    if component is LedgerComponent.Cpu:
        return Decimal(shape.cpu_millicores) * seconds / _MILLICORES_PER_CORE
    if component is LedgerComponent.Memory:
        return Decimal(shape.memory_mib) * seconds / _MIB_PER_GIB
    if component is LedgerComponent.Gpu:
        return Decimal(shape.gpu_count) * seconds
    raise ValueError(f"{component} is not capacity a placement reserves")


def measured_quantity(component: LedgerComponent, quantity: Decimal) -> Decimal:
    """What a window measured, in the component's quoted unit.

    Only the unit moves here. Memory arrives as byte-seconds and is quoted per
    gibibyte-second, and the division is a relative 1e-28 short of exact at the
    default decimal context — 1/2^30 needs thirty significant digits and the
    context keeps twenty-eight. Against a rate in the low thousands of
    nanodollars that cannot move the whole nanodollar a quote rounds to, and the
    remedy is never a float.

    The gVisor sentry's and gofer's own resident memory is inside this figure.
    That is management overhead the platform charges for, not an over-read to be
    subtracted: it exists because the container runs, it is small against a
    gibibyte-second, and netting it out would put a per-runtime correction in the
    middle of a price.
    """

    if component is LedgerComponent.Memory:
        return quantity / BYTES_PER_GIB
    if component in (
        LedgerComponent.Cpu,
        LedgerComponent.Egress,
        LedgerComponent.VolumeStorage,
    ):
        return quantity
    raise ValueError(f"{component} has no measured counterpart")


def measured_excess(measured: Decimal, floor: Decimal) -> Decimal:
    """What a window used above the capacity it held. Never negative.

    Under the floor there is nothing to add: the floor is already charged by the
    duration record covering the same window, so subtracting it here is what
    makes the two records sum to `max(reserved, measured)` rather than to their
    total.
    """

    return max(Decimal(0), measured - floor)


@dataclass(frozen=True, slots=True)
class Quote:
    """One rate, in force over one half-open interval.

    `rate_nanos_per_unit` has no default and admits no `None`. A quote that does
    not know its rate cannot be constructed, so no caller can read zero off one.
    """

    component: LedgerComponent
    rate_nanos_per_unit: Decimal
    pricing_version: str
    effective_at: datetime
    valid_until: datetime | None

    def __post_init__(self) -> None:
        _require_instant(self.effective_at, "effective_at")
        if self.valid_until is not None:
            _require_instant(self.valid_until, "valid_until")
            if self.valid_until <= self.effective_at:
                raise ValueError("a quote must stay in force after it takes effect")
        if self.rate_nanos_per_unit < 0:
            raise ValueError("a rate cannot be negative")

    def covers(self, at: datetime) -> bool:
        return self.effective_at <= at and (self.valid_until is None or at < self.valid_until)

    def cost_nanos(self, quantity: Decimal) -> int:
        product = self.rate_nanos_per_unit * quantity
        return int(product.quantize(_WHOLE_NANO, rounding=ROUND_HALF_EVEN))


@dataclass(frozen=True, slots=True)
class MeteredSpan:
    """One metering window's worth of one component, on one basis.

    Wall clock places the span; the quantity is what that component is billed for
    over it, which for capacity held is derived from the window and the placement
    and for capacity measured is what the window exceeded its floor by. The
    interval decides only how the quantity is split across rate changes.
    """

    component: LedgerComponent
    basis: LedgerBasis
    started_at: datetime
    ended_at: datetime
    quantity: Decimal

    def __post_init__(self) -> None:
        _require_instant(self.started_at, "started_at")
        _require_instant(self.ended_at, "ended_at")
        if self.ended_at <= self.started_at:
            raise ValueError("a metered span must end after it starts")
        if self.quantity < 0:
            raise ValueError("a metered span quantity cannot be negative")

    @property
    def dimension(self) -> BilledDimension:
        return _COMPONENT_DIMENSIONS[self.component]


@dataclass(frozen=True, slots=True)
class PricedSegment:
    index: int
    started_at: datetime
    ended_at: datetime
    duration_ms: int
    quantity: Decimal
    quote: Quote
    cost_nanos: int


@dataclass(frozen=True, slots=True)
class PricedSpan:
    segments: tuple[PricedSegment, ...]

    def __post_init__(self) -> None:
        if not self.segments:
            raise ValueError("a priced span covers at least one segment")

    @property
    def cost_nanos(self) -> int:
        return sum(segment.cost_nanos for segment in self.segments)


@dataclass(frozen=True, slots=True)
class UnpricedSpan:
    """No published rate covered part of the span.

    Carries no cost field at all, deliberately: there is no number on this type
    for a caller to mistake for zero.
    """

    dimension: BilledDimension
    gap_started_at: datetime
    gap_ended_at: datetime
    reason: UnpricedReason


SpanPricing: TypeAlias = PricedSpan | UnpricedSpan


def price_span(span: MeteredSpan, quotes: Sequence[Quote]) -> SpanPricing:
    """Split at quote boundaries and price each segment.

    Segments tile `[started_at, ended_at)` exactly. The boundary list begins at
    the span start, ends at the span end, and is strictly increasing after
    deduplication, so consecutive pairs leave no gap and no overlap by
    construction rather than by check. Integer milliseconds and apportioned
    quantity each sum back to the span's own totals because the final segment
    takes the remainder instead of its own rounded share.

    Apportioning by elapsed milliseconds is exact for capacity held, which is
    linear in time. For capacity measured it assumes the window's usage was
    uniform across it, which is the most a window with no sub-window samples can
    say, and it is the same assumption a rate boundary has always been split on.

    Giving the remainder to the last segment is what makes integer milliseconds
    sum back to the whole: every other split leaves the total short or long by
    the rounding error, and a billing total that does not reconcile with its own
    segments is the one arithmetic error a customer will find.
    """

    covering = sorted(
        (quote for quote in quotes if quote.component is span.component),
        key=lambda quote: quote.effective_at,
    )
    edges = {span.started_at, span.ended_at}
    for quote in covering:
        for edge in (quote.effective_at, quote.valid_until):
            if edge is not None and span.started_at < edge < span.ended_at:
                edges.add(edge)
    boundaries = sorted(edges)
    total_ms = (span.ended_at - span.started_at) // _MILLISECOND
    last = len(boundaries) - 2
    segments: list[PricedSegment] = []
    spent_ms = 0
    spent_quantity = Decimal(0)
    for index in range(len(boundaries) - 1):
        started_at, ended_at = boundaries[index], boundaries[index + 1]
        quote = _quote_at(covering, started_at)
        if quote is None:
            return UnpricedSpan(
                dimension=span.dimension,
                gap_started_at=started_at,
                gap_ended_at=ended_at,
                reason=UnpricedReason.NoPublishedRate,
            )
        if index == last:
            duration_ms = total_ms - spent_ms
            quantity = span.quantity - spent_quantity
        else:
            duration_ms = (ended_at - started_at) // _MILLISECOND
            # Apportioned from the cumulative share rather than this segment's
            # own, so each share is monotone in elapsed milliseconds and no
            # per-segment rounding accumulates across a long window.
            quantity = (
                _apportioned(span.quantity, spent_ms + duration_ms, total_ms) - spent_quantity
            )
        spent_ms += duration_ms
        spent_quantity += quantity
        segments.append(
            PricedSegment(
                index=index,
                started_at=started_at,
                ended_at=ended_at,
                duration_ms=duration_ms,
                quantity=quantity,
                quote=quote,
                cost_nanos=quote.cost_nanos(quantity),
            )
        )
    return PricedSpan(segments=tuple(segments))


def _apportioned(quantity: Decimal, consumed_ms: int, total_ms: int) -> Decimal:
    if total_ms <= 0:
        return Decimal(0)
    return quantity * consumed_ms / total_ms


def _quote_at(quotes: Sequence[Quote], at: datetime) -> Quote | None:
    for quote in quotes:
        if quote.covers(at):
            return quote
    return None


__all__ = [
    "BILLED_METRICS",
    "BYTES_PER_GIB",
    "NANOS_PER_USD",
    "BilledDimension",
    "BilledUsage",
    "ContainerShape",
    "LedgerBasis",
    "LedgerComponent",
    "MeteredSpan",
    "PricedSegment",
    "PricedSpan",
    "Quote",
    "SpanPricing",
    "UnpricedReason",
    "UnpricedSpan",
    "elapsed_seconds",
    "measured_excess",
    "measured_quantity",
    "price_span",
    "reserved_quantity",
]
