from __future__ import annotations

import csv
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from io import StringIO
from typing import cast

from database.records.apps import AppRecord, StubRecord
from database.repositories.usage_billing import (
    UsageBillingAggregateRow,
    UsageBillingEvidenceRow,
    UsageMetadataScalar,
)
from shared.billing import (
    PRICED_METRICS,
    BillableMetric,
    BillingCoverageStatus,
    SellPriceConfig,
)
from shared.gpu import SUPPORTED_GPU_NAMES, GpuType
from shared.timestamps import utc_now
from shared.usage import (
    UsageBillingOwner,
    UsageMetric,
    UsageUnit,
)


@dataclass(frozen=True, slots=True)
class SellPrice:
    """One rate LazyCloud charges, for one metric and optionally one variant.

    `variant` distinguishes rates that share a metric but not a price. GPU seconds
    are the case that forces it: a T4 second and an H100 second are both
    `gpu_seconds` and differ by more than an order of magnitude, and a single
    blended rate would leave a customer unable to see why their bill is what it
    is. An empty variant means the rate covers the whole metric.
    """

    metric: BillableMetric
    label: str
    unit: UsageUnit
    price_per_unit_nanos: int
    currency: str
    effective_date: date
    variant: str = ""
    note: str = ""

    @classmethod
    def from_config(cls, config: SellPriceConfig) -> SellPrice:
        return cls(
            metric=config.metric,
            label=config.label,
            unit=config.unit,
            price_per_unit_nanos=config.price_per_unit_nanos,
            currency=config.currency,
            effective_date=config.effective_date,
            variant=config.variant,
            note=config.note,
        )

    @property
    def key(self) -> tuple[BillableMetric, str]:
        return (self.metric, self.variant)


@dataclass(frozen=True, slots=True)
class UsagePriceCatalog:
    """The rates a workspace is billed against."""

    currency: str
    prices: tuple[SellPrice, ...]
    _by_key: Mapping[tuple[BillableMetric, str], tuple[SellPrice, ...]] = field(
        default_factory=lambda: cast("dict[tuple[BillableMetric, str], tuple[SellPrice, ...]]", {}),
        repr=False,
        compare=False,
    )
    """Built once in `__post_init__`, newest effective date first.

    A key holds a history rather than a rate, because a rate applies from its
    effective date forward and usage keeps whatever was in force when it ran.
    Every priced key in a report resolves through here, for the summary and again
    for each app, workload and activity bucket.
    """

    def __post_init__(self) -> None:
        currency = self.currency.strip().upper()
        if len(currency) != 3 or not currency.isalpha():
            raise ValueError("usage billing currency must be a three-letter code")
        object.__setattr__(self, "currency", currency)
        keys: set[tuple[BillableMetric, str]] = set()
        dated_keys: set[tuple[BillableMetric, str, date]] = set()
        dated: set[tuple[tuple[BillableMetric, str], date]] = set()
        metrics: set[BillableMetric] = set()
        for price in self.prices:
            if price.currency != currency:
                raise ValueError(
                    f"usage price for {price.metric.value} uses {price.currency}, "
                    f"expected {currency}"
                )
            if (price.key, price.effective_date) in dated:
                variant = f" variant {price.variant}" if price.variant else ""
                raise ValueError(
                    f"duplicate usage price for {price.metric.value}{variant} "
                    f"effective {price.effective_date.isoformat()}"
                )
            dated.add((price.key, price.effective_date))
            dated_keys.add((price.metric, price.variant, price.effective_date))
            keys.add(price.key)
            metrics.add(price.metric)
        required = set(PRICED_METRICS)
        if metrics != required:
            missing = ", ".join(metric.value for metric in PRICED_METRICS if metric not in metrics)
            unsupported = ", ".join(sorted(metric.value for metric in metrics - required))
            details: list[str] = []
            if missing:
                details.append(f"missing {missing}")
            if unsupported:
                details.append(f"unsupported {unsupported}")
            raise ValueError(
                f"usage price catalog must cover exactly the priced metrics: {'; '.join(details)}"
            )
        self._validate_gpu_variants(dated_keys, utc_now().date())
        history: dict[tuple[BillableMetric, str], tuple[SellPrice, ...]] = {}
        for price in self.prices:
            history[price.key] = (*history.get(price.key, ()), price)
        object.__setattr__(
            self,
            "_by_key",
            {
                key: tuple(sorted(rates, key=lambda rate: rate.effective_date, reverse=True))
                for key, rates in history.items()
            },
        )

    @staticmethod
    def _validate_gpu_variants(keys: set[tuple[BillableMetric, str, date]], today: date) -> None:
        """GPU rates must name exactly the models the platform schedules, today.

        Effectivity is part of it: a model whose only rate starts next month is
        priced by a catalog that satisfies a date check and bills nothing until
        then, which is the same silent gap an absent rate would be.

        Priced and schedulable are one list, `SUPPORTED_GPU_TYPES`. Left to drift
        they diverge in both directions and neither is visible: a model that runs
        with no rate bills nothing, and a rate for hardware nobody can rent looks
        like coverage that does not exist.

        A variant-free GPU rate is refused for the same reason. It answers for
        every model at once, which turns an unpriced GPU from a named gap into a
        silent charge at some other chip's price.
        """

        for metric in (BillableMetric.GpuSeconds, BillableMetric.ManagedGpuSeconds):
            priced = {
                variant
                for priced_metric, variant, effective_date in keys
                if priced_metric is metric and effective_date <= today
            }
            if "" in priced:
                raise ValueError(
                    f"{metric.value} must be priced per model; a rate with no model "
                    "would answer for every GPU including ones nobody priced"
                )
            if priced != SUPPORTED_GPU_NAMES:
                missing = ", ".join(sorted(SUPPORTED_GPU_NAMES - priced))
                unsupported = ", ".join(sorted(priced - SUPPORTED_GPU_NAMES))
                details: list[str] = []
                if missing:
                    details.append(f"schedulable but unpriced: {missing}")
                if unsupported:
                    details.append(f"priced but not schedulable: {unsupported}")
                raise ValueError(
                    f"{metric.value} prices disagree with SUPPORTED_GPU_TYPES "
                    f"({'; '.join(details)})"
                )

    def price_for(
        self,
        metric: BillableMetric,
        variant: str = "",
        *,
        on: date,
    ) -> SellPrice | None:
        """The rate in force for one metric and variant on a given day.

        `on` is the day the usage happened, not the day the report runs. A price
        change is prospective: it moves what tomorrow costs and never what
        yesterday did, so a month recomputed after a change still prices at the
        rate the customer was charged under.

        Usage predating every rate in the catalog resolves to `None`. Reaching
        back for the oldest rate would stamp a line with an effective date that
        postdates the work it priced, and an unpriced line is a gap the coverage
        report already names.

        Falls back to a metric's variant-free rate, which the constructor forbids
        for GPU seconds—so an unpriced GPU resolves to `None` and surfaces as a
        named gap rather than being billed at another chip's price.
        """

        found = self._effective(self._by_key.get((metric, variant)), on)
        if found is not None:
            return found
        return self._effective(self._by_key.get((metric, "")), on)

    @staticmethod
    def _effective(rates: tuple[SellPrice, ...] | None, on: date) -> SellPrice | None:
        """The newest rate that had taken effect by `on`."""

        if not rates:
            return None
        for rate in rates:
            if rate.effective_date <= on:
                return rate
        return None


@dataclass(frozen=True, slots=True)
class UsageBillingLine:
    metric: BillableMetric
    label: str
    quantity: float
    unit: UsageUnit
    price_per_unit_nanos: int | None
    cost_nanos: int
    variant: str = ""
    """Which rate produced this line—the GPU model, or empty where one rate covers
    the whole metric."""

    effective_date: date | None = None
    """When the rate that priced this line took effect, or null where nothing
    priced it.

    Carried because a rate change splits a metric into two lines rather than
    blending them, and without this the two are indistinguishable."""


@dataclass(frozen=True, slots=True)
class UsageBillingAttribution:
    app_id: str
    app_name: str
    workload_id: str
    workload_name: str
    workload_kind: str
    tasks: int
    total_cost_nanos: int
    lines: tuple[UsageBillingLine, ...]


@dataclass(frozen=True, slots=True)
class UsageBillingBucket:
    start: datetime
    end: datetime
    total_cost_nanos: int
    lines: tuple[UsageBillingLine, ...]


@dataclass(frozen=True, slots=True)
class UsageBillingCoverageGap:
    billable_metric: BillableMetric | None
    usage_metric: UsageMetric | None
    reason: str


@dataclass(frozen=True, slots=True)
class UsageBillingCoverage:
    status: BillingCoverageStatus
    priced_metrics: tuple[BillableMetric, ...]
    unpriced_metrics: tuple[BillableMetric, ...]
    omitted_duration_records: int
    gaps: tuple[UsageBillingCoverageGap, ...]


@dataclass(frozen=True, slots=True)
class UsageBillingReport:
    workspace_id: str
    start: datetime
    end: datetime
    currency: str
    total_cost_nanos: int
    catalog: tuple[SellPrice, ...]
    summary: tuple[UsageBillingLine, ...]
    apps: tuple[UsageBillingAttribution, ...]
    workloads: tuple[UsageBillingAttribution, ...]
    activity: tuple[UsageBillingBucket, ...]
    coverage: UsageBillingCoverage


@dataclass(frozen=True, slots=True)
class UsageBillingAppSummary:
    app_id: str
    app_name: str
    tasks: int
    total_cost_nanos: int
    lines: tuple[UsageBillingLine, ...]


@dataclass(frozen=True, slots=True)
class UsageBillingOverview:
    workspace_id: str
    start: datetime
    end: datetime
    currency: str
    total_cost_nanos: int
    summary: tuple[UsageBillingLine, ...]
    apps: tuple[UsageBillingAppSummary, ...]
    activity: tuple[UsageBillingBucket, ...]


@dataclass(frozen=True, slots=True)
class UsageBillingWorkloads:
    workspace_id: str
    app_id: str
    start: datetime
    end: datetime
    currency: str
    data: tuple[UsageBillingAttribution, ...]


_WindowKey = tuple[BillableMetric, str]
"""What a window accumulates against: a metric and its variant, before any rate
has been chosen. The rate cannot be part of it, because the window is still being
built when quantities land in it."""

_PricedKey = tuple[BillableMetric, str, date | None]
"""What one billing line is: a metric, the variant that prices it — the GPU model,
or empty for the rest — and the effective date of the rate that priced it.

The date is part of the identity because a rate change splits a line rather than
blending it: a month spanning one shows both rates, which is what the customer
was actually charged."""


@dataclass(slots=True)
class _ComputeWindow:
    priced_on: date
    """The day this window's usage happened, which decides the rate it prices at.

    Held because the report's own interval cannot answer it: a month spans rate
    changes, and every window in it has to price at what was in force when it
    ran rather than at what is in force when the report is built.
    """

    direct_quantities: dict[_WindowKey, float] = field(default_factory=dict)
    derived_quantities: dict[_WindowKey, float] = field(default_factory=dict)
    billing_owner: str = ""
    """Who paid for the machine this window ran on.

    Held per window rather than per record, and resolved the same way the rollup
    resolves it — the greatest label wins, so a stated classification beats an
    unstated one. A window whose records disagreed would otherwise bill its
    derived seconds against one metric and its direct seconds against another,
    which defeats the direct-over-derived precedence below and charges the same
    second twice.
    """

    def merge(self, other: _ComputeWindow) -> None:
        for key, quantity in other.direct_quantities.items():
            self.direct_quantities[key] = self.direct_quantities.get(key, 0) + quantity
        for key, quantity in other.derived_quantities.items():
            self.derived_quantities[key] = self.derived_quantities.get(key, 0) + quantity
        self.billing_owner = max(self.billing_owner, other.billing_owner)
        # Earliest wins: a window that merged across a boundary prices at the
        # rate its usage started under, never at a later and possibly higher one.
        self.priced_on = min(self.priced_on, other.priced_on)


@dataclass(frozen=True, slots=True)
class _MeteringWindowKey:
    resource_id: str
    worker_id: str
    window_start_ms: int | None
    window_end_ms: int | None
    legacy_record_id: str | None


@dataclass(slots=True)
class _UsageAccumulator:
    compute_windows: dict[_MeteringWindowKey, _ComputeWindow] = field(default_factory=dict)
    tasks: int = 0
    omitted_duration_records: int = 0

    def merge(self, other: _UsageAccumulator) -> None:
        for window_key, window in other.compute_windows.items():
            self.compute_windows.setdefault(
                window_key, _ComputeWindow(priced_on=window.priced_on)
            ).merge(window)
        self.tasks += other.tasks
        self.omitted_duration_records += other.omitted_duration_records


_PRICES_EFFECTIVE = date(2026, 8, 11)

_GPU_SELL_PRICES: tuple[tuple[str, int], ...] = (
    (GpuType.T4.value, 155_800),
    (GpuType.L4.value, 249_833),
    (GpuType.A10G.value, 333_667),
    (GpuType.A100_40.value, 553_850),
    (GpuType.L40S.value, 593_985),
    (GpuType.A100_80.value, 812_674),
    (GpuType.H100.value, 936_700),
    (GpuType.H200.value, 1_088_399),
)
"""Nanodollars per GPU-second, by GPU model, ascending.

Set at `max(competitor_anchor x 0.95, aws_cost x 1.5)` against AWS us-east-1
rates observed 2026-08-11.

The datacenter parts—L40S and above—are derived from spot and are therefore
preemptible by default. On-demand they land 2.1x-3.3x above Modal and Beam,
neither of which buys on-demand either, and pricing only the flagships off spot
produced an inversion where an H100 second cost less than an A100-80 second. The
G-family parts stay on-demand: spot saves little there and on-demand capacity is
easier to hold.

A GPU absent from this table is unpriced rather than billed at a neighbouring
chip's rate.
"""

_MANAGED_GPU_FEES: tuple[tuple[str, int], ...] = (
    (GpuType.T4.value, 7_300),
    (GpuType.L4.value, 13_496),
    (GpuType.A10G.value, 17_967),
    (GpuType.L40S.value, 35_751),
    (GpuType.A100_40.value, 70_556),
    (GpuType.A100_80.value, 93_333),
    (GpuType.H100.value, 234_667),
    (GpuType.H200.value, 278_000),
)
"""Nanodollars per managed GPU-second, by model, ascending.

Each is 8% of that model's AWS us-east-1 on-demand price per GPU-second, taken
from the smallest instance offering it and net of the vCPU and memory that
instance also carries, since those bill under their own managed rates. Netted
dollars per GPU-hour: T4 0.3285 (g4dn.xlarge), L4 0.6073 (g6.xlarge), A10G
0.8085 (g5.xlarge), L40S 1.6088 (g6e.xlarge), A100-40 3.175 (p4d.24xlarge),
A100-80 4.200 (p4de.24xlarge), H100 10.5575 (p5.48xlarge), H200 12.50875
(p5e.48xlarge). The vCPU and memory subtracted are the rates below.

Do not derive these from `_GPU_SELL_PRICES`. That table is
`max(competitor x 0.95, aws_cost x 1.5)` and its datacenter rows price off spot,
so it carries neither an on-demand figure nor a consistent multiple of one.

Every rate here is netted from an observed on-demand price. A model nobody can
net that way does not belong in this table, and therefore does not belong in
`SUPPORTED_GPU_TYPES` either — the two are one list, so a card that cannot be
priced is a card this platform does not offer.
"""

DEFAULT_SELL_PRICES: tuple[SellPrice, ...] = (
    SellPrice(
        metric=BillableMetric.CpuSeconds,
        label="CPU",
        unit=UsageUnit.Seconds,
        price_per_unit_nanos=15_313,
        currency="USD",
        effective_date=_PRICES_EFFECTIVE,
    ),
    SellPrice(
        metric=BillableMetric.MemoryGibSeconds,
        label="Memory",
        unit=UsageUnit.GibSeconds,
        price_per_unit_nanos=2_100,
        currency="USD",
        effective_date=_PRICES_EFFECTIVE,
    ),
    *(
        SellPrice(
            metric=BillableMetric.GpuSeconds,
            label=f"GPU ({gpu})",
            unit=UsageUnit.Seconds,
            price_per_unit_nanos=nanos,
            currency="USD",
            effective_date=_PRICES_EFFECTIVE,
            variant=gpu,
        )
        for gpu, nanos in _GPU_SELL_PRICES
    ),
    SellPrice(
        metric=BillableMetric.ManagedCpuSeconds,
        label="Managed CPU",
        unit=UsageUnit.Seconds,
        # 8% of $0.0357 per vCPU-hour, solved from c5.xlarge against r5.xlarge.
        price_per_unit_nanos=793,
        currency="USD",
        effective_date=_PRICES_EFFECTIVE,
    ),
    SellPrice(
        metric=BillableMetric.ManagedMemoryGibSeconds,
        label="Managed memory",
        unit=UsageUnit.GibSeconds,
        # 8% of $0.00342 per GiB-hour, from the same pair.
        price_per_unit_nanos=76,
        currency="USD",
        effective_date=_PRICES_EFFECTIVE,
    ),
    *(
        SellPrice(
            metric=BillableMetric.ManagedGpuSeconds,
            label=f"Managed GPU ({gpu})",
            unit=UsageUnit.Seconds,
            price_per_unit_nanos=nanos,
            currency="USD",
            effective_date=_PRICES_EFFECTIVE,
            variant=gpu,
        )
        for gpu, nanos in _MANAGED_GPU_FEES
    ),
)


def default_usage_price_catalog() -> UsagePriceCatalog:
    return UsagePriceCatalog(currency="USD", prices=DEFAULT_SELL_PRICES)


def configured_usage_price_catalog(
    *,
    currency: str,
    prices: Iterable[SellPriceConfig] | None,
) -> UsagePriceCatalog:
    if prices is None:
        catalog = default_usage_price_catalog()
        if catalog.currency != currency.strip().upper():
            raise ValueError(
                "usage billing currency requires an explicit matching usage price catalog"
            )
        return catalog
    return UsagePriceCatalog(
        currency=currency,
        prices=tuple(SellPrice.from_config(price) for price in prices),
    )


_LINE_ORDER: Mapping[BillableMetric, int] = {
    metric: order for order, metric in enumerate(BillableMetric)
}
"""Where each metric sits on a bill, in declaration order.

Derived rather than listed so a metric cannot be priced without being sortable:
every lookup here is unguarded, and a metric the catalog demands but this map
omits raises on the first line it produces.
"""


def build_usage_billing_report(
    *,
    workspace_id: str,
    records: Iterable[UsageBillingEvidenceRow],
    apps: Iterable[AppRecord],
    stubs: Iterable[StubRecord],
    start: datetime,
    end: datetime,
    bucket_seconds: int,
    price_catalog: UsagePriceCatalog | None = None,
) -> UsageBillingReport:
    catalog = price_catalog or default_usage_price_catalog()
    prices = catalog
    app_names = {app.id: app.name for app in apps}
    stub_records = {stub.id: stub for stub in stubs}
    buckets: dict[tuple[datetime, str, str], _UsageAccumulator] = {}
    for record in records:
        billing_moment = _billing_moment(record)
        if billing_moment < start or billing_moment >= end:
            continue
        bucket_start = _bucket_start(billing_moment, bucket_seconds, origin=start)
        workload_id = record.workload_id
        if not workload_id and record.metric is UsageMetric.TaskCount:
            workload_id = record.resource_id
        key = (
            bucket_start,
            record.app_id,
            workload_id,
        )
        _accumulate_record(buckets.setdefault(key, _UsageAccumulator()), record)

    summary_accumulator = _UsageAccumulator()
    app_accumulators: dict[str, _UsageAccumulator] = {}
    workload_accumulators: dict[tuple[str, str], _UsageAccumulator] = {}
    bucket_accumulators: dict[datetime, _UsageAccumulator] = {}
    for (bucket_start, app_id, workload_id), accumulator in buckets.items():
        summary_accumulator.merge(accumulator)
        app_accumulators.setdefault(app_id, _UsageAccumulator()).merge(accumulator)
        workload_accumulators.setdefault((app_id, workload_id), _UsageAccumulator()).merge(
            accumulator
        )
        bucket_accumulators.setdefault(bucket_start, _UsageAccumulator()).merge(accumulator)

    summary = _billing_lines(summary_accumulator, prices)
    app_rows = tuple(
        _attribution(
            accumulator,
            app_id=app_id,
            app_name=app_names.get(app_id, "Unlinked"),
            prices=prices,
        )
        for app_id, accumulator in sorted(
            app_accumulators.items(),
            key=lambda item: (
                -_total_cost(_billing_lines(item[1], prices)),
                app_names.get(item[0], "Unlinked"),
            ),
        )
        if _has_usage(accumulator)
    )
    workload_rows = tuple(
        _attribution(
            accumulator,
            app_id=app_id,
            app_name=app_names.get(app_id, "Unlinked"),
            workload_id=workload_id,
            workload_name=(
                stub_records[workload_id].name
                if workload_id in stub_records
                else "Unlinked workload"
            ),
            workload_kind=(
                stub_records[workload_id].kind.value if workload_id in stub_records else ""
            ),
            prices=prices,
        )
        for (app_id, workload_id), accumulator in sorted(
            workload_accumulators.items(),
            key=lambda item: (
                -_total_cost(_billing_lines(item[1], prices)),
                app_names.get(item[0][0], "Unlinked"),
                stub_records[item[0][1]].name
                if item[0][1] in stub_records
                else "Unlinked workload",
            ),
        )
        if _has_usage(accumulator)
    )
    activity = tuple(
        UsageBillingBucket(
            start=bucket_start,
            end=min(bucket_start + timedelta(seconds=bucket_seconds), end),
            total_cost_nanos=_total_cost(lines),
            lines=lines,
        )
        for bucket_start in _bucket_range(start, end, bucket_seconds)
        for lines in (
            _billing_lines(bucket_accumulators.get(bucket_start, _UsageAccumulator()), prices),
        )
    )
    coverage = _billing_coverage(summary_accumulator, prices)
    return UsageBillingReport(
        workspace_id=workspace_id,
        start=start,
        end=end,
        currency=catalog.currency,
        total_cost_nanos=_total_cost(summary),
        catalog=catalog.prices,
        summary=summary,
        apps=app_rows,
        workloads=workload_rows,
        activity=activity,
        coverage=coverage,
    )


def build_usage_billing_overview(
    *,
    workspace_id: str,
    records: Iterable[UsageBillingAggregateRow],
    apps: Iterable[AppRecord],
    start: datetime,
    end: datetime,
    bucket_seconds: int,
    price_catalog: UsagePriceCatalog | None = None,
) -> UsageBillingOverview:
    catalog = price_catalog or default_usage_price_catalog()
    prices = catalog
    app_names = {app.id: app.name for app in apps}
    summary_accumulator = _UsageAccumulator()
    app_accumulators: dict[str, _UsageAccumulator] = {}
    bucket_accumulators: dict[datetime, _UsageAccumulator] = {}
    for record in records:
        accumulator = _aggregate_accumulator(record)
        summary_accumulator.merge(accumulator)
        app_accumulators.setdefault(record.app_id, _UsageAccumulator()).merge(accumulator)
        bucket_accumulators.setdefault(record.bucket_start, _UsageAccumulator()).merge(accumulator)

    summary = _billing_lines(summary_accumulator, prices)
    apps_rows = tuple(
        _app_summary(
            accumulator,
            app_id=app_id,
            app_name=app_names.get(app_id, "Unlinked"),
            prices=prices,
        )
        for app_id, accumulator in sorted(
            app_accumulators.items(),
            key=lambda item: (
                -_total_cost(_billing_lines(item[1], prices)),
                app_names.get(item[0], "Unlinked"),
            ),
        )
        if _has_usage(accumulator)
    )
    activity = tuple(
        UsageBillingBucket(
            start=bucket_start,
            end=min(bucket_start + timedelta(seconds=bucket_seconds), end),
            total_cost_nanos=_total_cost(lines),
            lines=lines,
        )
        for bucket_start in _bucket_range(start, end, bucket_seconds)
        for lines in (
            _billing_lines(bucket_accumulators.get(bucket_start, _UsageAccumulator()), prices),
        )
    )
    return UsageBillingOverview(
        workspace_id=workspace_id,
        start=start,
        end=end,
        currency=catalog.currency,
        total_cost_nanos=_total_cost(summary),
        summary=summary,
        apps=apps_rows,
        activity=activity,
    )


def build_usage_billing_workloads(
    *,
    workspace_id: str,
    app_id: str,
    records: Iterable[UsageBillingAggregateRow],
    apps: Iterable[AppRecord],
    stubs: Iterable[StubRecord],
    start: datetime,
    end: datetime,
    price_catalog: UsagePriceCatalog | None = None,
) -> UsageBillingWorkloads:
    catalog = price_catalog or default_usage_price_catalog()
    prices = catalog
    app_names = {app.id: app.name for app in apps}
    stub_records = {stub.id: stub for stub in stubs}
    accumulators: dict[str, _UsageAccumulator] = {}
    for record in records:
        accumulators.setdefault(record.workload_id, _UsageAccumulator()).merge(
            _aggregate_accumulator(record)
        )
    data = tuple(
        _attribution(
            accumulator,
            app_id=app_id,
            app_name=app_names.get(app_id, "Unlinked"),
            workload_id=workload_id,
            workload_name=(
                stub_records[workload_id].name
                if workload_id in stub_records
                else "Unlinked workload"
            ),
            workload_kind=(
                stub_records[workload_id].kind.value if workload_id in stub_records else ""
            ),
            prices=prices,
        )
        for workload_id, accumulator in sorted(
            accumulators.items(),
            key=lambda item: (
                -_total_cost(_billing_lines(item[1], prices)),
                stub_records[item[0]].name if item[0] in stub_records else "Unlinked workload",
            ),
        )
        if _has_usage(accumulator)
    )
    return UsageBillingWorkloads(
        workspace_id=workspace_id,
        app_id=app_id,
        start=start,
        end=end,
        currency=catalog.currency,
        data=data,
    )


_MANAGED_METRICS: Mapping[BillableMetric, BillableMetric] = {
    BillableMetric.CpuSeconds: BillableMetric.ManagedCpuSeconds,
    BillableMetric.MemoryGibSeconds: BillableMetric.ManagedMemoryGibSeconds,
    BillableMetric.GpuSeconds: BillableMetric.ManagedGpuSeconds,
}


def _metric_trio(billing_owner: str) -> Mapping[BillableMetric, BillableMetric]:
    """How this window's catalog metrics translate to what it is billed under.

    Capacity on a customer's own cloud account bills a management fee instead of
    a compute rate, and the two are different metrics so one report can show both
    when an app runs on both fleets. Anything else — the platform fleet, and rows
    written before the classification existed — bills at catalog rates.
    """

    if billing_owner == UsageBillingOwner.ConnectedCloud.value:
        return _MANAGED_METRICS
    return {}


def _aggregate_accumulator(record: UsageBillingAggregateRow) -> _UsageAccumulator:
    accumulator = _UsageAccumulator(
        tasks=record.runs,
        omitted_duration_records=record.omitted_duration_records,
    )
    quantities: dict[_WindowKey, float] = {
        (BillableMetric.CpuSeconds, ""): record.cpu_seconds,
        (BillableMetric.MemoryGibSeconds, ""): record.memory_gib_seconds,
        (BillableMetric.GpuSeconds, record.gpu_type): record.gpu_seconds,
    }
    if any(quantity > 0 for quantity in quantities.values()):
        key = _MeteringWindowKey(
            # One app, workload and bucket can return several rows differing only
            # by GPU model, by billing owner, or by the day they ran. All three
            # are part of this key because all three change which rate applies:
            # rows sharing it collide and their quantities sum, which is only
            # correct where they would price identically. The day matters even
            # when the report asks for a single bucket — especially then, since
            # every row in it shares one bucket start.
            resource_id=(
                f"{record.app_id}:{record.workload_id}:"
                f"{record.bucket_start.isoformat()}:{record.gpu_type}:"
                f"{record.billing_owner}:{record.priced_on.isoformat()}"
            ),
            worker_id="",
            window_start_ms=None,
            window_end_ms=None,
            legacy_record_id=None,
        )
        accumulator.compute_windows[key] = _ComputeWindow(
            direct_quantities={
                key: quantity for key, quantity in quantities.items() if quantity > 0
            },
            billing_owner=record.billing_owner,
            priced_on=record.priced_on,
        )
    return accumulator


def _accumulate_record(
    accumulator: _UsageAccumulator,
    record: UsageBillingEvidenceRow,
) -> None:
    quantity = max(record.quantity, 0)
    if _self_hosted_container_metric(record):
        return
    if record.metric is UsageMetric.ContainerDurationMilliseconds:
        compute_window = _compute_window(accumulator, record)
        seconds = quantity / 1_000
        resources = {
            _window_key(BillableMetric.CpuSeconds, record): seconds
            * _nonnegative_number(record.cpu_millicores)
            / 1_000,
            _window_key(BillableMetric.MemoryGibSeconds, record): seconds
            * _nonnegative_number(record.memory_mb)
            / 1_024,
            _window_key(BillableMetric.GpuSeconds, record): seconds
            * _nonnegative_number(record.gpu_count),
        }
        if not any(value > 0 for value in resources.values()):
            accumulator.omitted_duration_records += 1
        for key, derived_quantity in resources.items():
            compute_window.derived_quantities[key] = (
                compute_window.derived_quantities.get(key, 0) + derived_quantity
            )
        return
    direct_metric = {
        UsageMetric.CpuSeconds: BillableMetric.CpuSeconds,
        UsageMetric.MemoryGibSeconds: BillableMetric.MemoryGibSeconds,
        UsageMetric.GpuSeconds: BillableMetric.GpuSeconds,
    }.get(record.metric)
    if direct_metric is not None:
        compute_window = _compute_window(accumulator, record)
        key = _window_key(direct_metric, record)
        compute_window.direct_quantities[key] = (
            compute_window.direct_quantities.get(key, 0) + quantity
        )
        return
    if record.metric is UsageMetric.TaskCount:
        accumulator.tasks += round(quantity)
        return


def _billing_lines(
    accumulator: _UsageAccumulator,
    prices: UsagePriceCatalog,
) -> tuple[UsageBillingLine, ...]:
    lines: list[UsageBillingLine] = []
    for key, (quantity, price) in _compute_quantities(accumulator, prices).items():
        if quantity <= 0:
            continue
        metric, variant, effective_date = key
        if price is None:
            # A null price says why the cost is zero.
            lines.append(
                UsageBillingLine(
                    metric=metric,
                    variant=variant,
                    label=_unpriced_label(key),
                    quantity=quantity,
                    unit=_METRIC_UNITS[metric],
                    price_per_unit_nanos=None,
                    cost_nanos=0,
                    effective_date=effective_date,
                )
            )
            continue
        lines.append(
            UsageBillingLine(
                metric=metric,
                variant=variant,
                label=price.label,
                quantity=quantity,
                unit=price.unit,
                price_per_unit_nanos=price.price_per_unit_nanos,
                cost_nanos=_estimated_cost_nanos(price, quantity),
                effective_date=effective_date,
            )
        )
    return tuple(
        sorted(
            lines,
            key=lambda line: (
                _LINE_ORDER[line.metric],
                line.variant,
                line.effective_date or date.min,
            ),
        )
    )


def _attribution(
    accumulator: _UsageAccumulator,
    *,
    app_id: str,
    app_name: str,
    prices: UsagePriceCatalog,
    workload_id: str = "",
    workload_name: str = "",
    workload_kind: str = "",
) -> UsageBillingAttribution:
    lines = _billing_lines(accumulator, prices)
    return UsageBillingAttribution(
        app_id=app_id,
        app_name=app_name,
        workload_id=workload_id,
        workload_name=workload_name,
        workload_kind=workload_kind,
        tasks=accumulator.tasks,
        total_cost_nanos=_total_cost(lines),
        lines=lines,
    )


def _app_summary(
    accumulator: _UsageAccumulator,
    *,
    app_id: str,
    app_name: str,
    prices: UsagePriceCatalog,
) -> UsageBillingAppSummary:
    lines = _billing_lines(accumulator, prices)
    return UsageBillingAppSummary(
        app_id=app_id,
        app_name=app_name,
        tasks=accumulator.tasks,
        total_cost_nanos=_total_cost(lines),
        lines=lines,
    )


def _billing_coverage(
    accumulator: _UsageAccumulator,
    prices: UsagePriceCatalog,
) -> UsageBillingCoverage:
    compute_quantities = _compute_quantities(accumulator, prices)
    observed_keys = {key for key, (quantity, _) in compute_quantities.items() if quantity > 0}
    unpriced_keys = {
        key
        for key, (quantity, price) in compute_quantities.items()
        if quantity > 0 and price is None
    }
    observed = {key[0] for key in observed_keys}
    unpriced = {key[0] for key in unpriced_keys}
    priced = observed - unpriced
    gaps = [
        UsageBillingCoverageGap(
            billable_metric=key[0],
            usage_metric=None,
            # Naming the variant matters: "no price for gpu_seconds" sends someone
            # looking for a missing catalog entry that is in fact present for every
            # GPU but the one that ran.
            reason=(
                f"No configured price is available for {key[0].value} ({key[1]})."
                if key[1]
                else f"No configured price is available for {key[0].value}."
            ),
        )
        for key in sorted(unpriced_keys, key=lambda item: (_LINE_ORDER[item[0]], item[1]))
    ]
    if accumulator.omitted_duration_records:
        gaps.append(
            UsageBillingCoverageGap(
                billable_metric=None,
                usage_metric=UsageMetric.ContainerDurationMilliseconds,
                reason=(
                    "Container duration records without CPU, memory, or GPU resource labels "
                    "cannot be priced."
                ),
            )
        )
    has_billable_evidence = bool(observed or accumulator.omitted_duration_records)
    if not has_billable_evidence:
        status = BillingCoverageStatus.Empty
    elif not priced:
        status = BillingCoverageStatus.Unpriced
    elif gaps:
        status = BillingCoverageStatus.Partial
    else:
        status = BillingCoverageStatus.Complete
    return UsageBillingCoverage(
        status=status,
        priced_metrics=tuple(sorted(priced, key=lambda item: _LINE_ORDER[item])),
        unpriced_metrics=tuple(sorted(unpriced, key=lambda item: _LINE_ORDER[item])),
        omitted_duration_records=accumulator.omitted_duration_records,
        gaps=tuple(gaps),
    )


_CSV_FIELDS: tuple[str, ...] = (
    "section",
    "workspace_id",
    "window_start",
    "window_end",
    "currency",
    "total_cost_nanos",
    "bucket_start",
    "bucket_end",
    "app_id",
    "app_name",
    "workload_id",
    "workload_name",
    "workload_kind",
    "tasks",
    "metric",
    "label",
    "quantity",
    "unit",
    "price_per_unit_nanos",
    "cost_nanos",
    "variant",
    "effective_date",
    "note",
    "coverage_status",
    "priced_metrics",
    "unpriced_metrics",
    "omitted_duration_records",
    "billable_metric",
    "usage_metric",
    "reason",
)


def usage_billing_report_csv(report: UsageBillingReport) -> str:
    output = StringIO(newline="")
    writer: csv.DictWriter[str] = csv.DictWriter(output, fieldnames=_CSV_FIELDS)
    writer.writeheader()
    writer.writerow(_csv_base(report, section="report"))
    for price in report.catalog:
        writer.writerow(
            {
                **_csv_base(report, section="catalog"),
                "metric": price.metric.value,
                "label": price.label,
                "unit": price.unit.value,
                "price_per_unit_nanos": price.price_per_unit_nanos,
                "variant": price.variant,
                "effective_date": price.effective_date.isoformat(),
                "note": price.note,
            }
        )
    for line in report.summary:
        writer.writerow(_csv_line(report, section="summary", line=line))
    for item in report.apps:
        _write_attribution_rows(writer, report, section="app", item=item)
    for item in report.workloads:
        _write_attribution_rows(writer, report, section="workload", item=item)
    for bucket in report.activity:
        if not bucket.lines:
            writer.writerow(
                {
                    **_csv_base(report, section="activity"),
                    "bucket_start": bucket.start.isoformat(),
                    "bucket_end": bucket.end.isoformat(),
                    "total_cost_nanos": bucket.total_cost_nanos,
                }
            )
        for line in bucket.lines:
            writer.writerow(
                {
                    **_csv_line(report, section="activity", line=line),
                    "bucket_start": bucket.start.isoformat(),
                    "bucket_end": bucket.end.isoformat(),
                    "total_cost_nanos": bucket.total_cost_nanos,
                }
            )
    writer.writerow(
        {
            **_csv_base(report, section="coverage"),
            "coverage_status": report.coverage.status.value,
            "priced_metrics": "|".join(metric.value for metric in report.coverage.priced_metrics),
            "unpriced_metrics": "|".join(
                metric.value for metric in report.coverage.unpriced_metrics
            ),
            "omitted_duration_records": report.coverage.omitted_duration_records,
        }
    )
    for gap in report.coverage.gaps:
        writer.writerow(
            {
                **_csv_base(report, section="coverage_gap"),
                "coverage_status": report.coverage.status.value,
                "billable_metric": gap.billable_metric.value if gap.billable_metric else "",
                "usage_metric": gap.usage_metric.value if gap.usage_metric else "",
                "reason": gap.reason,
            }
        )
    return output.getvalue()


def _write_attribution_rows(
    writer: csv.DictWriter[str],
    report: UsageBillingReport,
    *,
    section: str,
    item: UsageBillingAttribution,
) -> None:
    attribution: dict[str, str | int] = {
        "app_id": item.app_id,
        "app_name": item.app_name,
        "workload_id": item.workload_id,
        "workload_name": item.workload_name,
        "workload_kind": item.workload_kind,
        "tasks": item.tasks,
        "total_cost_nanos": item.total_cost_nanos,
    }
    if not item.lines:
        writer.writerow({**_csv_base(report, section=section), **attribution})
    for line in item.lines:
        writer.writerow(
            {
                **_csv_line(report, section=section, line=line),
                **attribution,
            }
        )


def _csv_base(report: UsageBillingReport, *, section: str) -> dict[str, str | int | bool]:
    return {
        "section": section,
        "workspace_id": report.workspace_id,
        "window_start": report.start.isoformat(),
        "window_end": report.end.isoformat(),
        "currency": report.currency,
        "total_cost_nanos": report.total_cost_nanos,
    }


def _csv_line(
    report: UsageBillingReport,
    *,
    section: str,
    line: UsageBillingLine,
) -> dict[str, str | int | float | bool | None]:
    return {
        **_csv_base(report, section=section),
        "metric": line.metric.value,
        "variant": line.variant,
        "label": line.label,
        "quantity": line.quantity,
        "unit": line.unit.value,
        "price_per_unit_nanos": line.price_per_unit_nanos,
        "cost_nanos": line.cost_nanos,
        "effective_date": line.effective_date.isoformat() if line.effective_date else "",
    }


_METRIC_UNITS: Mapping[BillableMetric, UsageUnit] = {
    BillableMetric.CpuSeconds: UsageUnit.Seconds,
    BillableMetric.MemoryGibSeconds: UsageUnit.GibSeconds,
    BillableMetric.GpuSeconds: UsageUnit.Seconds,
    BillableMetric.ManagedCpuSeconds: UsageUnit.Seconds,
    BillableMetric.ManagedMemoryGibSeconds: UsageUnit.GibSeconds,
    BillableMetric.ManagedGpuSeconds: UsageUnit.Seconds,
}


def _unpriced_label(key: _PricedKey) -> str:
    metric, variant, _ = key
    base = metric.value.replace("_", " ").capitalize()
    return f"{base} ({variant})" if variant else base


def _estimated_cost_nanos(price: SellPrice, quantity: float) -> int:
    return int(
        (Decimal(str(quantity)) * Decimal(price.price_per_unit_nanos)).quantize(
            Decimal("1"), rounding=ROUND_HALF_UP
        )
    )


def _nonnegative_number(value: str) -> float:
    try:
        return max(float(value or "0"), 0)
    except ValueError:
        return 0


def _compute_window(
    accumulator: _UsageAccumulator,
    record: UsageBillingEvidenceRow,
) -> _ComputeWindow:
    key = _metering_window_key(record)
    moment = _billing_moment(record).date()
    window = accumulator.compute_windows.get(key)
    if window is None:
        window = _ComputeWindow(priced_on=moment)
        accumulator.compute_windows[key] = window
    else:
        window.priced_on = min(window.priced_on, moment)
    window.billing_owner = max(window.billing_owner, record.billing_owner)
    return window


def _metering_window_key(record: UsageBillingEvidenceRow) -> _MeteringWindowKey:
    window_start_ms = _metadata_int(record.window_start_ms)
    window_end_ms = _metadata_int(record.window_end_ms)
    if (
        window_start_ms is None
        or window_end_ms is None
        or window_start_ms < 0
        or window_end_ms <= window_start_ms
    ):
        return _MeteringWindowKey(
            resource_id=record.resource_id,
            worker_id="",
            window_start_ms=None,
            window_end_ms=None,
            legacy_record_id=record.id,
        )
    worker_id = record.metadata_worker_id or record.label_worker_id
    return _MeteringWindowKey(
        resource_id=record.resource_id,
        worker_id=worker_id,
        window_start_ms=window_start_ms,
        window_end_ms=window_end_ms,
        legacy_record_id=None,
    )


def _metadata_int(value: UsageMetadataScalar) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return None
    return None


def _billing_moment(record: UsageBillingEvidenceRow) -> datetime:
    value = record.metering_window_started_at
    if value:
        try:
            window_started_at = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return record.created_at
    else:
        return record.created_at
    if window_started_at.tzinfo is None or window_started_at.utcoffset() is None:
        return record.created_at
    return window_started_at.astimezone(UTC)


def _self_hosted_container_metric(record: UsageBillingEvidenceRow) -> bool:
    return (
        record.resource_type == "container"
        and record.billing_owner == UsageBillingOwner.SelfHosted.value
    ) and record.metric in {
        UsageMetric.ContainerDurationMilliseconds,
        UsageMetric.CpuSeconds,
        UsageMetric.MemoryGibSeconds,
        UsageMetric.GpuSeconds,
    }


def _window_key(metric: BillableMetric, record: UsageBillingEvidenceRow) -> _WindowKey:
    """Which rate this record's contribution is billed against.

    Only GPU seconds vary by model, managed or not. Everything else prices the
    same whatever hardware produced it.
    """

    if metric in {BillableMetric.GpuSeconds, BillableMetric.ManagedGpuSeconds}:
        return (metric, record.gpu)
    return (metric, "")


def _compute_quantities(
    accumulator: _UsageAccumulator,
    prices: UsagePriceCatalog,
) -> dict[_PricedKey, tuple[float, SellPrice | None]]:
    """Total each priced key, taking direct measurement over derived per window.

    Windows accumulate under the catalog metrics whatever paid for them, and the
    management fee is applied here, once, from the window's own owner. Choosing
    the metric while records arrive would let one window hold both, and a direct
    second under one metric can never take precedence over a derived second under
    another — it would be added to it.
    """

    quantities: dict[_PricedKey, tuple[float, SellPrice | None]] = {}
    for window in accumulator.compute_windows.values():
        metrics = _metric_trio(window.billing_owner)
        for key in set(window.direct_quantities) | set(window.derived_quantities):
            metric, variant = key
            billed = metrics.get(metric, metric)
            # Resolved per window, so usage either side of a rate change keeps
            # its own rate and lands on its own line rather than being blended
            # into an average nobody was ever charged.
            rate = prices.price_for(billed, variant, on=window.priced_on)
            priced = (billed, variant, rate.effective_date if rate is not None else None)
            quantity = (
                window.direct_quantities[key]
                if key in window.direct_quantities
                else window.derived_quantities.get(key, 0)
            )
            running = quantities.get(priced, (0.0, None))[0]
            quantities[priced] = (running + quantity, rate)
    return quantities


def _bucket_start(moment: datetime, bucket_seconds: int, *, origin: datetime) -> datetime:
    aware = moment.astimezone(UTC)
    elapsed_seconds = max(int((aware - origin).total_seconds()), 0)
    return origin + timedelta(seconds=elapsed_seconds - elapsed_seconds % bucket_seconds)


def _bucket_range(start: datetime, end: datetime, bucket_seconds: int) -> tuple[datetime, ...]:
    buckets: list[datetime] = []
    cursor = start
    while cursor < end:
        buckets.append(cursor)
        cursor += timedelta(seconds=bucket_seconds)
    return tuple(buckets)


def _total_cost(lines: Iterable[UsageBillingLine]) -> int:
    return sum(line.cost_nanos for line in lines)


def _has_usage(accumulator: _UsageAccumulator) -> bool:
    return bool(
        accumulator.compute_windows or accumulator.tasks or accumulator.omitted_duration_records
    )


__all__ = [
    "UsageBillingAppSummary",
    "UsageBillingAttribution",
    "UsageBillingBucket",
    "UsageBillingCoverage",
    "UsageBillingCoverageGap",
    "UsageBillingLine",
    "UsageBillingOverview",
    "UsageBillingReport",
    "UsageBillingWorkloads",
    "UsagePriceCatalog",
    "build_usage_billing_overview",
    "build_usage_billing_report",
    "build_usage_billing_workloads",
    "configured_usage_price_catalog",
    "default_usage_price_catalog",
    "usage_billing_report_csv",
]
