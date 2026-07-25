from __future__ import annotations

import csv
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from io import StringIO

from database.records.apps import AppRecord, StubRecord
from database.repositories.usage_billing import (
    UsageBillingAggregateRow,
    UsageBillingEvidenceRow,
    UsageMetadataScalar,
)
from shared.billing import (
    COMPUTE_PRICE_METRICS,
    BillableMetric,
    BillingCostBasis,
    BillingCoverageStatus,
    UsagePriceConfig,
)
from shared.usage import (
    UsageBillingOwner,
    UsageMetric,
    UsageUnit,
)

NANOS_PER_MAJOR_CURRENCY_UNIT = 1_000_000_000
AWS_FARGATE_PRICING_URL = (
    "https://aws.amazon.com/about-aws/whats-new/2019/01/"
    "aws-fargate-prices-reduced-by-up-to-50-percent/"
)
AWS_G4_REFERENCE_URL = (
    "https://aws.amazon.com/blogs/machine-learning/"
    "bert-inference-on-g4-instances-using-apache-mxnet-and-gluonnlp-"
    "1-million-requests-for-20-cents/"
)


@dataclass(frozen=True, slots=True)
class UsagePrice:
    metric: BillableMetric
    label: str
    unit: UsageUnit
    price_per_unit_nanos: int
    currency: str
    provider: str
    service: str
    region: str
    effective_date: date
    source_url: str
    note: str = ""

    @classmethod
    def from_config(cls, config: UsagePriceConfig) -> UsagePrice:
        return cls(
            metric=config.metric,
            label=config.label,
            unit=config.unit,
            price_per_unit_nanos=config.price_per_unit_nanos,
            currency=config.currency,
            provider=config.provider,
            service=config.service,
            region=config.region,
            effective_date=config.effective_date,
            source_url=config.source_url,
            note=config.note,
        )


@dataclass(frozen=True, slots=True)
class UsagePriceCatalog:
    currency: str
    prices: tuple[UsagePrice, ...]

    def __post_init__(self) -> None:
        currency = self.currency.strip().upper()
        if len(currency) != 3 or not currency.isalpha():
            raise ValueError("usage billing currency must be a three-letter code")
        object.__setattr__(self, "currency", currency)
        metrics: set[BillableMetric] = set()
        for price in self.prices:
            if price.currency != currency:
                raise ValueError(
                    f"usage price for {price.metric.value} uses {price.currency}, "
                    f"expected {currency}"
                )
            if price.metric in metrics:
                raise ValueError(f"duplicate usage price for {price.metric.value}")
            metrics.add(price.metric)
        if metrics != set(COMPUTE_PRICE_METRICS):
            missing = ", ".join(
                metric.value for metric in COMPUTE_PRICE_METRICS if metric not in metrics
            )
            unsupported = ", ".join(
                sorted(metric.value for metric in metrics - set(COMPUTE_PRICE_METRICS))
            )
            details: list[str] = []
            if missing:
                details.append(f"missing {missing}")
            if unsupported:
                details.append(f"unsupported {unsupported}")
            raise ValueError(
                f"usage price catalog must cover exactly the compute metrics: {'; '.join(details)}"
            )

    @property
    def by_metric(self) -> Mapping[BillableMetric, UsagePrice]:
        return {price.metric: price for price in self.prices}


@dataclass(frozen=True, slots=True)
class UsageBillingLine:
    metric: BillableMetric
    label: str
    quantity: float
    unit: UsageUnit
    price_per_unit_nanos: int | None
    cost_nanos: int
    cost_basis: BillingCostBasis


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
    recorded_cost_present: bool
    omitted_duration_records: int
    gaps: tuple[UsageBillingCoverageGap, ...]


@dataclass(frozen=True, slots=True)
class UsageBillingReport:
    workspace_id: str
    start: datetime
    end: datetime
    currency: str
    total_cost_nanos: int
    contains_estimates: bool
    catalog: tuple[UsagePrice, ...]
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
    contains_estimates: bool
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


@dataclass(slots=True)
class _ComputeWindow:
    direct_quantities: dict[BillableMetric, float] = field(default_factory=dict)
    derived_quantities: dict[BillableMetric, float] = field(default_factory=dict)
    recorded_container_cost_nanos: int = 0

    def merge(self, other: _ComputeWindow) -> None:
        for metric, quantity in other.direct_quantities.items():
            self.direct_quantities[metric] = self.direct_quantities.get(metric, 0) + quantity
        for metric, quantity in other.derived_quantities.items():
            self.derived_quantities[metric] = self.derived_quantities.get(metric, 0) + quantity
        self.recorded_container_cost_nanos += other.recorded_container_cost_nanos


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
    managed_cost_nanos: int = 0
    managed_seconds: float = 0
    customer_cloud_management_cost_nanos: int = 0
    customer_cloud_management_seconds: float = 0
    tasks: int = 0
    omitted_duration_records: int = 0

    def merge(self, other: _UsageAccumulator) -> None:
        for window_key, window in other.compute_windows.items():
            self.compute_windows.setdefault(window_key, _ComputeWindow()).merge(window)
        self.managed_cost_nanos += other.managed_cost_nanos
        self.managed_seconds += other.managed_seconds
        self.customer_cloud_management_cost_nanos += other.customer_cloud_management_cost_nanos
        self.customer_cloud_management_seconds += other.customer_cloud_management_seconds
        self.tasks += other.tasks
        self.omitted_duration_records += other.omitted_duration_records


REFERENCE_PRICES: tuple[UsagePrice, ...] = (
    UsagePrice(
        metric=BillableMetric.CpuSeconds,
        label="CPU",
        unit=UsageUnit.Seconds,
        price_per_unit_nanos=11_244,
        currency="USD",
        provider="AWS",
        service="Fargate Linux/x86",
        region="us-east-1",
        effective_date=date(2019, 1, 7),
        source_url=AWS_FARGATE_PRICING_URL,
        note=(
            "Configured AWS reference rate from the 2019-01-07 Fargate price schedule; "
            "free tiers and discounts are excluded."
        ),
    ),
    UsagePrice(
        metric=BillableMetric.MemoryGibSeconds,
        label="Memory",
        unit=UsageUnit.GibSeconds,
        price_per_unit_nanos=1_235,
        currency="USD",
        provider="AWS",
        service="Fargate Linux/x86",
        region="us-east-1",
        effective_date=date(2019, 1, 7),
        source_url=AWS_FARGATE_PRICING_URL,
        note=(
            "Configured AWS reference rate from the 2019-01-07 Fargate price schedule; "
            "free tiers and discounts are excluded."
        ),
    ),
    UsagePrice(
        metric=BillableMetric.GpuSeconds,
        label="GPU",
        unit=UsageUnit.Seconds,
        price_per_unit_nanos=146_111,
        currency="USD",
        provider="AWS",
        service="EC2 g4dn.xlarge / NVIDIA T4",
        region="us-east-1",
        effective_date=date(2020, 9, 28),
        source_url=AWS_G4_REFERENCE_URL,
        note="Historical AWS reference instance rate converted to one GPU-second.",
    ),
)


def default_usage_price_catalog() -> UsagePriceCatalog:
    return UsagePriceCatalog(currency="USD", prices=REFERENCE_PRICES)


def configured_usage_price_catalog(
    *,
    currency: str,
    prices: Iterable[UsagePriceConfig] | None,
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
        prices=tuple(UsagePrice.from_config(price) for price in prices),
    )


_DERIVED_METRICS = COMPUTE_PRICE_METRICS
_LINE_ORDER = {
    BillableMetric.CpuSeconds: 0,
    BillableMetric.MemoryGibSeconds: 1,
    BillableMetric.GpuSeconds: 2,
    BillableMetric.RecordedCompute: 3,
    BillableMetric.ManagedCompute: 4,
    BillableMetric.CustomerCloudManagement: 5,
}


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
    prices = catalog.by_metric
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
        contains_estimates=any(
            line.cost_basis is not BillingCostBasis.Recorded for line in summary
        ),
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
    prices = catalog.by_metric
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
        contains_estimates=any(
            line.cost_basis is not BillingCostBasis.Recorded for line in summary
        ),
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
    prices = catalog.by_metric
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


def _aggregate_accumulator(record: UsageBillingAggregateRow) -> _UsageAccumulator:
    accumulator = _UsageAccumulator(
        managed_cost_nanos=record.managed_compute_cost_nanos,
        managed_seconds=record.managed_compute_seconds,
        customer_cloud_management_cost_nanos=record.customer_cloud_management_cost_nanos,
        customer_cloud_management_seconds=record.customer_cloud_management_seconds,
        tasks=record.runs,
        omitted_duration_records=record.omitted_duration_records,
    )
    quantities = {
        BillableMetric.CpuSeconds: record.cpu_seconds,
        BillableMetric.MemoryGibSeconds: record.memory_gib_seconds,
        BillableMetric.GpuSeconds: record.gpu_seconds,
    }
    if record.recorded_compute_cost_nanos > 0 or any(
        quantity > 0 for quantity in quantities.values()
    ):
        key = _MeteringWindowKey(
            resource_id=f"{record.app_id}:{record.workload_id}:{record.bucket_start.isoformat()}",
            worker_id="",
            window_start_ms=None,
            window_end_ms=None,
            legacy_record_id=None,
        )
        accumulator.compute_windows[key] = _ComputeWindow(
            direct_quantities={
                metric: quantity for metric, quantity in quantities.items() if quantity > 0
            },
            recorded_container_cost_nanos=record.recorded_compute_cost_nanos,
        )
    return accumulator


def _accumulate_record(
    accumulator: _UsageAccumulator,
    record: UsageBillingEvidenceRow,
) -> None:
    quantity = max(record.quantity, 0)
    if _managed_reservation_container_metric(record):
        return
    if record.metric is UsageMetric.ContainerDurationMilliseconds:
        compute_window = _compute_window(accumulator, record)
        seconds = quantity / 1_000
        resources = {
            BillableMetric.CpuSeconds: seconds * _nonnegative_number(record.cpu_millicores) / 1_000,
            BillableMetric.MemoryGibSeconds: seconds
            * _nonnegative_number(record.memory_mb)
            / 1_024,
            BillableMetric.GpuSeconds: seconds * _nonnegative_number(record.gpu_count),
        }
        if not any(value > 0 for value in resources.values()):
            accumulator.omitted_duration_records += 1
        for metric, derived_quantity in resources.items():
            compute_window.derived_quantities[metric] = (
                compute_window.derived_quantities.get(metric, 0) + derived_quantity
            )
        return
    direct_metric = {
        UsageMetric.CpuSeconds: BillableMetric.CpuSeconds,
        UsageMetric.MemoryGibSeconds: BillableMetric.MemoryGibSeconds,
        UsageMetric.GpuSeconds: BillableMetric.GpuSeconds,
    }.get(record.metric)
    if direct_metric is not None:
        compute_window = _compute_window(accumulator, record)
        compute_window.direct_quantities[direct_metric] = (
            compute_window.direct_quantities.get(direct_metric, 0) + quantity
        )
        return
    if record.metric is UsageMetric.TaskCount:
        accumulator.tasks += round(quantity)
        return
    if record.metric is UsageMetric.ContainerCostCents:
        _compute_window(accumulator, record).recorded_container_cost_nanos += _cents_to_nanos(
            quantity
        )
        return
    if record.metric is UsageMetric.ManagedComputeReservationSeconds:
        accumulator.managed_seconds += quantity
        return
    if record.metric is UsageMetric.ManagedComputeReservationCostCents:
        accumulator.managed_cost_nanos += _cents_to_nanos(quantity)
        return
    if record.metric is UsageMetric.CustomerCloudManagementSeconds:
        accumulator.customer_cloud_management_seconds += quantity
        return
    if record.metric is UsageMetric.CustomerCloudManagementCostCents:
        accumulator.customer_cloud_management_cost_nanos += _cents_to_nanos(quantity)
        return


def _billing_lines(
    accumulator: _UsageAccumulator,
    prices: Mapping[BillableMetric, UsagePrice],
) -> tuple[UsageBillingLine, ...]:
    lines: list[UsageBillingLine] = []
    compute_quantities = _compute_quantities(accumulator)
    recorded_container_cost_nanos = _recorded_container_cost_nanos(accumulator)
    estimated_compute_costs = {
        metric: _estimated_cost_nanos(prices[metric], quantity)
        for metric, quantity in compute_quantities.items()
        if quantity > 0 and metric in prices
    }
    if recorded_container_cost_nanos > 0 and estimated_compute_costs:
        compute_costs = _allocate_cost(
            recorded_container_cost_nanos,
            estimated_compute_costs,
        )
        compute_basis = BillingCostBasis.RecordedAllocation
    else:
        compute_costs = estimated_compute_costs
        compute_basis = BillingCostBasis.CatalogEstimate
    for metric, quantity in compute_quantities.items():
        if quantity <= 0:
            continue
        price = prices.get(metric)
        if price is None:
            continue
        lines.append(
            UsageBillingLine(
                metric=metric,
                label=price.label,
                quantity=quantity,
                unit=price.unit,
                price_per_unit_nanos=price.price_per_unit_nanos,
                cost_nanos=compute_costs.get(metric, 0),
                cost_basis=compute_basis,
            )
        )
    if recorded_container_cost_nanos > 0 and not estimated_compute_costs:
        lines.append(
            UsageBillingLine(
                metric=BillableMetric.RecordedCompute,
                label="Recorded compute",
                quantity=(recorded_container_cost_nanos * 100 / NANOS_PER_MAJOR_CURRENCY_UNIT),
                unit=UsageUnit.Cents,
                price_per_unit_nanos=None,
                cost_nanos=recorded_container_cost_nanos,
                cost_basis=BillingCostBasis.Recorded,
            )
        )
    if accumulator.managed_cost_nanos > 0:
        lines.append(
            UsageBillingLine(
                metric=BillableMetric.ManagedCompute,
                label="Managed compute",
                quantity=accumulator.managed_seconds,
                unit=UsageUnit.Seconds,
                price_per_unit_nanos=None,
                cost_nanos=accumulator.managed_cost_nanos,
                cost_basis=BillingCostBasis.Recorded,
            )
        )
    if accumulator.customer_cloud_management_cost_nanos > 0:
        lines.append(
            UsageBillingLine(
                metric=BillableMetric.CustomerCloudManagement,
                label="Customer cloud management",
                quantity=accumulator.customer_cloud_management_seconds,
                unit=UsageUnit.Seconds,
                price_per_unit_nanos=None,
                cost_nanos=accumulator.customer_cloud_management_cost_nanos,
                cost_basis=BillingCostBasis.Recorded,
            )
        )
    return tuple(sorted(lines, key=lambda line: _LINE_ORDER[line.metric]))


def _attribution(
    accumulator: _UsageAccumulator,
    *,
    app_id: str,
    app_name: str,
    prices: Mapping[BillableMetric, UsagePrice],
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
    prices: Mapping[BillableMetric, UsagePrice],
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
    prices: Mapping[BillableMetric, UsagePrice],
) -> UsageBillingCoverage:
    compute_quantities = _compute_quantities(accumulator)
    observed = {metric for metric, quantity in compute_quantities.items() if quantity > 0}
    if accumulator.managed_seconds > 0 or accumulator.managed_cost_nanos > 0:
        observed.add(BillableMetric.ManagedCompute)
    if (
        accumulator.customer_cloud_management_seconds > 0
        or accumulator.customer_cloud_management_cost_nanos > 0
    ):
        observed.add(BillableMetric.CustomerCloudManagement)

    priced = {
        metric
        for metric in observed
        if metric in prices
        or (metric is BillableMetric.ManagedCompute and accumulator.managed_cost_nanos > 0)
        or (
            metric is BillableMetric.CustomerCloudManagement
            and accumulator.customer_cloud_management_cost_nanos > 0
        )
    }
    unpriced = observed - priced
    gaps = [
        UsageBillingCoverageGap(
            billable_metric=metric,
            usage_metric=None,
            reason=f"No configured price is available for {metric.value}.",
        )
        for metric in sorted(unpriced, key=lambda item: _LINE_ORDER[item])
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
    recorded_cost_present = (
        _recorded_container_cost_nanos(accumulator) > 0
        or accumulator.managed_cost_nanos > 0
        or accumulator.customer_cloud_management_cost_nanos > 0
    )
    has_billable_evidence = bool(
        observed or recorded_cost_present or accumulator.omitted_duration_records
    )
    if not has_billable_evidence:
        status = BillingCoverageStatus.Empty
    elif not priced and not recorded_cost_present:
        status = BillingCoverageStatus.Unpriced
    elif gaps:
        status = BillingCoverageStatus.Partial
    else:
        status = BillingCoverageStatus.Complete
    return UsageBillingCoverage(
        status=status,
        priced_metrics=tuple(sorted(priced, key=lambda item: _LINE_ORDER[item])),
        unpriced_metrics=tuple(sorted(unpriced, key=lambda item: _LINE_ORDER[item])),
        recorded_cost_present=recorded_cost_present,
        omitted_duration_records=accumulator.omitted_duration_records,
        gaps=tuple(gaps),
    )


_CSV_FIELDS: tuple[str, ...] = (
    "section",
    "workspace_id",
    "window_start",
    "window_end",
    "currency",
    "contains_estimates",
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
    "cost_basis",
    "provider",
    "service",
    "region",
    "effective_date",
    "source_url",
    "note",
    "coverage_status",
    "priced_metrics",
    "unpriced_metrics",
    "recorded_cost_present",
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
                "provider": price.provider,
                "service": price.service,
                "region": price.region,
                "effective_date": price.effective_date.isoformat(),
                "source_url": price.source_url,
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
            "recorded_cost_present": str(report.coverage.recorded_cost_present).lower(),
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
        "contains_estimates": report.contains_estimates,
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
        "label": line.label,
        "quantity": line.quantity,
        "unit": line.unit.value,
        "price_per_unit_nanos": line.price_per_unit_nanos,
        "cost_nanos": line.cost_nanos,
        "cost_basis": line.cost_basis.value,
    }


def _allocate_cost(
    total_cost_nanos: int,
    weights: Mapping[BillableMetric, int],
) -> dict[BillableMetric, int]:
    total_weight = sum(weights.values())
    if total_weight <= 0:
        return {}
    allocations: dict[BillableMetric, int] = {}
    remaining = total_cost_nanos
    ordered = sorted(weights, key=lambda metric: _LINE_ORDER[metric])
    for metric in ordered[:-1]:
        allocation = total_cost_nanos * weights[metric] // total_weight
        allocations[metric] = allocation
        remaining -= allocation
    allocations[ordered[-1]] = remaining
    return allocations


def _estimated_cost_nanos(price: UsagePrice, quantity: float) -> int:
    return int(
        (Decimal(str(quantity)) * Decimal(price.price_per_unit_nanos)).quantize(
            Decimal("1"), rounding=ROUND_HALF_UP
        )
    )


def _cents_to_nanos(quantity: float) -> int:
    return int(
        (Decimal(str(quantity)) * Decimal(NANOS_PER_MAJOR_CURRENCY_UNIT) / Decimal(100)).quantize(
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
    return accumulator.compute_windows.setdefault(key, _ComputeWindow())


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


def _managed_reservation_container_metric(record: UsageBillingEvidenceRow) -> bool:
    return (
        record.resource_type == "container"
        and record.billing_owner == UsageBillingOwner.ManagedReservation.value
    ) and record.metric in {
        UsageMetric.ContainerDurationMilliseconds,
        UsageMetric.ContainerCostCents,
        UsageMetric.CpuSeconds,
        UsageMetric.MemoryGibSeconds,
        UsageMetric.GpuSeconds,
    }


def _compute_quantities(accumulator: _UsageAccumulator) -> dict[BillableMetric, float]:
    quantities = {metric: 0.0 for metric in _DERIVED_METRICS}
    for window in accumulator.compute_windows.values():
        for metric in _DERIVED_METRICS:
            quantities[metric] += (
                window.direct_quantities[metric]
                if metric in window.direct_quantities
                else window.derived_quantities.get(metric, 0)
            )
    return quantities


def _recorded_container_cost_nanos(accumulator: _UsageAccumulator) -> int:
    return sum(
        window.recorded_container_cost_nanos for window in accumulator.compute_windows.values()
    )


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
        accumulator.compute_windows
        or accumulator.tasks
        or accumulator.managed_cost_nanos
        or accumulator.managed_seconds
        or accumulator.customer_cloud_management_cost_nanos
        or accumulator.customer_cloud_management_seconds
        or accumulator.omitted_duration_records
    )


__all__ = [
    "REFERENCE_PRICES",
    "UsageBillingAppSummary",
    "UsageBillingAttribution",
    "UsageBillingBucket",
    "UsageBillingCoverage",
    "UsageBillingCoverageGap",
    "UsageBillingLine",
    "UsageBillingOverview",
    "UsageBillingReport",
    "UsageBillingWorkloads",
    "UsagePrice",
    "UsagePriceCatalog",
    "build_usage_billing_overview",
    "build_usage_billing_report",
    "build_usage_billing_workloads",
    "configured_usage_price_catalog",
    "default_usage_price_catalog",
    "usage_billing_report_csv",
]
