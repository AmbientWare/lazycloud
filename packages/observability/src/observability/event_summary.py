from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
from enum import StrEnum

from pydantic import Field, JsonValue, field_validator
from shared.contracts import ContractModel
from shared.events import Event


class CoverageStatus(StrEnum):
    Full = "full"
    Partial = "partial"
    Missing = "missing"


class ContainerEventsBatchTarget(ContractModel):
    container_id: str | None = None
    task_id: str | None = None
    stub_id: str | None = None

    @field_validator("container_id", "task_id", "stub_id")
    @classmethod
    def _trim_blank(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        return stripped or None

    @property
    def key(self) -> tuple[str | None, str | None, str | None]:
        return (self.container_id, self.task_id, self.stub_id)


class ContainerEventsBatchRequest(ContractModel):
    container_ids: list[str] = Field(default_factory=list)
    task_ids: list[str] = Field(default_factory=list)
    targets: list[ContainerEventsBatchTarget] = Field(default_factory=list)
    limit: int | None = None
    event_types: list[str] = Field(default_factory=list)
    include_events: bool = False
    top_lifecycle: int = 20
    required_lifecycle_ids: list[str] = Field(default_factory=list)
    required_metrics: list[str] = Field(default_factory=list)

    @field_validator("limit", "top_lifecycle")
    @classmethod
    def _positive_optional(cls, value: int | None) -> int | None:
        if value is not None and value <= 0:
            msg = "event batch limits must be positive"
            raise ValueError(msg)
        return value


class ContainerLifecycleMetric(ContractModel):
    event_id: str
    duration_ms: int
    start_time: datetime | None = None
    end_time: datetime | None = None
    attrs: dict[str, str] = Field(default_factory=dict)


class ContainerMetricSummary(ContractModel):
    count: int
    min_ms: int
    avg_ms: float
    p50_ms: float
    p90_ms: float
    p95_ms: float
    p99_ms: float
    max_ms: int
    total_ms: int


class ContainerPhaseSummary(ContainerMetricSummary):
    metric_key: str
    event_id: str
    label: str = ""
    count: int
    coverage: float
    coverage_status: CoverageStatus
    rollup: bool = False


class ContainerEventSummary(ContractModel):
    container_id: str
    task_id: str | None = None
    stub_id: str | None = None
    event_count: int
    summary: dict[str, int] = Field(default_factory=dict)
    lifecycle: tuple[ContainerLifecycleMetric, ...] = ()
    slowest_lifecycle: tuple[ContainerLifecycleMetric, ...] = ()
    missing: tuple[str, ...] = ()
    events: tuple[Event, ...] = ()
    error: str | None = None


class ContainerEventsCoverage(ContractModel):
    requested_containers: int
    items: int
    containers_with_events: int
    event_errors: int = 0
    missing_containers: tuple[str, ...] = ()
    required_lifecycle_present: int = 0
    required_lifecycle_total: int = 0
    required_lifecycle_missing: dict[str, int] = Field(default_factory=dict)
    required_metric_present: int = 0
    required_metric_total: int = 0
    required_metric_missing: dict[str, int] = Field(default_factory=dict)


class ContainerEventsBatchResponse(ContractModel):
    count: int
    items: tuple[ContainerEventSummary, ...]
    summary: dict[str, ContainerMetricSummary] = Field(default_factory=dict)
    coverage: ContainerEventsCoverage
    phases: tuple[ContainerPhaseSummary, ...] = ()
    top_bottlenecks: tuple[ContainerPhaseSummary, ...] = ()
    primary_bottleneck: ContainerPhaseSummary | None = None


class PhaseDefinition(ContractModel):
    metric_key: str
    event_id: str
    label: str
    rollup: bool = False


PHASE_DEFINITIONS: tuple[PhaseDefinition, ...] = (
    PhaseDefinition(
        metric_key="scheduler_ms",
        event_id="scheduler",
        label="Scheduler total",
        rollup=True,
    ),
    PhaseDefinition(
        metric_key="scheduler_backlog_ms",
        event_id="scheduler.backlog",
        label="Scheduler backlog",
    ),
    PhaseDefinition(
        metric_key="worker_queue_ms",
        event_id="worker.queue",
        label="Worker queue",
    ),
    PhaseDefinition(
        metric_key="worker_receive_to_running_ms",
        event_id="worker.receive_to_running",
        label="Worker receive to running",
    ),
    PhaseDefinition(
        metric_key="image_load_ms",
        event_id="image.load",
        label="Image load",
    ),
    PhaseDefinition(
        metric_key="network_setup_ms",
        event_id="network.setup",
        label="Network setup",
    ),
    PhaseDefinition(
        metric_key="runtime_startup_ms",
        event_id="runtime.startup",
        label="Runtime startup",
    ),
    PhaseDefinition(
        metric_key="runner_execution_ms",
        event_id="runner.execution",
        label="Runner execution",
    ),
    PhaseDefinition(
        metric_key="result_delivery_ms",
        event_id="result.delivery",
        label="Result delivery",
    ),
)


def normalize_event_types(values: Iterable[str]) -> tuple[str, ...]:
    return _dedupe_trimmed(values)


def normalize_batch_targets(
    request: ContainerEventsBatchRequest,
) -> tuple[ContainerEventsBatchTarget, ...]:
    targets: list[ContainerEventsBatchTarget] = []
    targets.extend(request.targets)
    targets.extend(ContainerEventsBatchTarget(container_id=item) for item in request.container_ids)
    targets.extend(ContainerEventsBatchTarget(task_id=item) for item in request.task_ids)
    seen: set[tuple[str | None, str | None, str | None]] = set()
    normalized: list[ContainerEventsBatchTarget] = []
    for target in targets:
        if target.key == (None, None, None) or target.key in seen:
            continue
        seen.add(target.key)
        normalized.append(target)
    return tuple(normalized)


def build_container_events_batch_response(
    events: Iterable[Event],
    request: ContainerEventsBatchRequest,
) -> ContainerEventsBatchResponse:
    event_list = list(events)
    targets = normalize_batch_targets(request)
    if not targets:
        targets = tuple(
            ContainerEventsBatchTarget(container_id=container_id)
            for container_id in _dedupe_trimmed(_event_container_id(event) for event in event_list)
        )
    filtered_events = _filter_event_types(event_list, normalize_event_types(request.event_types))
    items = tuple(_summarize_target(target, filtered_events, request) for target in targets)
    coverage = _coverage(items, request)
    metric_summary = _metric_summary(items)
    phases = _phase_summaries(metric_summary, item_count=len(items))
    top_bottlenecks = tuple(
        sorted(
            (phase for phase in phases if phase.count > 0),
            key=lambda item: item.p95_ms,
            reverse=True,
        )[: request.top_lifecycle]
    )
    return ContainerEventsBatchResponse(
        count=len(items),
        items=items,
        summary=metric_summary,
        coverage=coverage,
        phases=phases,
        top_bottlenecks=top_bottlenecks,
        primary_bottleneck=top_bottlenecks[0] if top_bottlenecks else None,
    )


def _summarize_target(
    target: ContainerEventsBatchTarget,
    events: list[Event],
    request: ContainerEventsBatchRequest,
) -> ContainerEventSummary:
    target_events = [event for event in events if _matches_target(event, target)]
    target_events.sort(key=lambda event: event.created_at)
    if request.limit is not None:
        target_events = target_events[-request.limit :]
    lifecycle_values = [_lifecycle_metric(event) for event in target_events]
    lifecycle = tuple(metric for metric in lifecycle_values if metric is not None)
    summary = _summary_from_lifecycle(lifecycle)
    required_lifecycle = normalize_event_types(request.required_lifecycle_ids)
    present_lifecycle = {metric.event_id for metric in lifecycle}
    missing = tuple(
        event_id for event_id in required_lifecycle if event_id not in present_lifecycle
    )
    container_id = (
        target.container_id
        or _first_value(_event_container_id(event) for event in target_events)
        or ""
    )
    task_id = target.task_id or _first_value(_event_task_id(event) for event in target_events)
    stub_id = target.stub_id or _first_value(_event_stub_id(event) for event in target_events)
    return ContainerEventSummary(
        container_id=container_id,
        task_id=task_id,
        stub_id=stub_id,
        event_count=len(target_events),
        summary=summary,
        lifecycle=lifecycle,
        slowest_lifecycle=tuple(sorted(lifecycle, key=lambda item: item.duration_ms, reverse=True)),
        missing=missing,
        events=tuple(target_events) if request.include_events else (),
    )


def _lifecycle_metric(event: Event) -> ContainerLifecycleMetric | None:
    duration = event.data.get("duration_ms")
    if not isinstance(duration, int | float):
        return None
    event_id = str(event.data.get("event_id") or event.action)
    attrs = {
        str(key): str(value)
        for key, value in event.data.items()
        if key not in {"duration_ms", "event_id", "start_time", "end_time"}
        and isinstance(value, str | int | float | bool)
    }
    return ContainerLifecycleMetric(
        event_id=event_id,
        duration_ms=int(duration),
        start_time=_data_datetime(event.data.get("start_time")) or event.created_at,
        end_time=_data_datetime(event.data.get("end_time")) or event.created_at,
        attrs=attrs,
    )


def _data_datetime(value: JsonValue | datetime) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str) and value:
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    return None


def _summary_from_lifecycle(metrics: Iterable[ContainerLifecycleMetric]) -> dict[str, int]:
    summary: dict[str, int] = {}
    for metric in metrics:
        summary[_metric_key(metric.event_id)] = metric.duration_ms
    return summary


def _coverage(
    items: tuple[ContainerEventSummary, ...],
    request: ContainerEventsBatchRequest,
) -> ContainerEventsCoverage:
    required_lifecycle = normalize_event_types(request.required_lifecycle_ids)
    required_metrics = normalize_event_types(request.required_metrics)
    lifecycle_missing = {event_id: 0 for event_id in required_lifecycle}
    metric_missing = {metric_key: 0 for metric_key in required_metrics}
    for item in items:
        present_lifecycle = {metric.event_id for metric in item.lifecycle}
        for event_id in required_lifecycle:
            if event_id not in present_lifecycle:
                lifecycle_missing[event_id] += 1
        for metric_key in required_metrics:
            if metric_key not in item.summary:
                metric_missing[metric_key] += 1
    requested_total = len(items)
    lifecycle_total = requested_total * len(required_lifecycle)
    metric_total = requested_total * len(required_metrics)
    lifecycle_missing_count = sum(lifecycle_missing.values())
    metric_missing_count = sum(metric_missing.values())
    return ContainerEventsCoverage(
        requested_containers=requested_total,
        items=len(items),
        containers_with_events=sum(1 for item in items if item.event_count > 0),
        event_errors=sum(1 for item in items if item.error is not None),
        missing_containers=tuple(item.container_id for item in items if item.event_count == 0),
        required_lifecycle_present=lifecycle_total - lifecycle_missing_count,
        required_lifecycle_total=lifecycle_total,
        required_lifecycle_missing={
            key: value for key, value in lifecycle_missing.items() if value
        },
        required_metric_present=metric_total - metric_missing_count,
        required_metric_total=metric_total,
        required_metric_missing={key: value for key, value in metric_missing.items() if value},
    )


def _metric_summary(
    items: Iterable[ContainerEventSummary],
) -> dict[str, ContainerMetricSummary]:
    values: dict[str, list[int]] = {}
    for item in items:
        for key, value in item.summary.items():
            values.setdefault(key, []).append(value)
    return {key: _summarize_numbers(metric_values) for key, metric_values in sorted(values.items())}


def _phase_summaries(
    summaries: dict[str, ContainerMetricSummary],
    *,
    item_count: int,
) -> tuple[ContainerPhaseSummary, ...]:
    definitions = {definition.metric_key: definition for definition in PHASE_DEFINITIONS}
    phases: list[ContainerPhaseSummary] = []
    for metric_key, summary in summaries.items():
        definition = definitions.get(
            metric_key,
            PhaseDefinition(
                metric_key=metric_key,
                event_id=metric_key.removesuffix("_ms").replace("_", "."),
                label=metric_key.removesuffix("_ms").replace("_", " ").title(),
            ),
        )
        coverage = summary.count / item_count if item_count else 0
        phases.append(
            ContainerPhaseSummary(
                count=summary.count,
                min_ms=summary.min_ms,
                avg_ms=summary.avg_ms,
                p50_ms=summary.p50_ms,
                p90_ms=summary.p90_ms,
                p95_ms=summary.p95_ms,
                p99_ms=summary.p99_ms,
                max_ms=summary.max_ms,
                total_ms=summary.total_ms,
                metric_key=metric_key,
                event_id=definition.event_id,
                label=definition.label,
                coverage=coverage,
                coverage_status=_coverage_status(coverage),
                rollup=definition.rollup,
            )
        )
    return tuple(sorted(phases, key=lambda item: item.metric_key))


def _summarize_numbers(values: list[int]) -> ContainerMetricSummary:
    ordered = sorted(values)
    total = sum(ordered)
    return ContainerMetricSummary(
        count=len(ordered),
        min_ms=ordered[0],
        avg_ms=total / len(ordered),
        p50_ms=_percentile(ordered, 50),
        p90_ms=_percentile(ordered, 90),
        p95_ms=_percentile(ordered, 95),
        p99_ms=_percentile(ordered, 99),
        max_ms=ordered[-1],
        total_ms=total,
    )


def _percentile(values: list[int], percentile: float) -> float:
    if len(values) == 1:
        return float(values[0])
    rank = (len(values) - 1) * (percentile / 100)
    lower = int(rank)
    upper = min(lower + 1, len(values) - 1)
    weight = rank - lower
    return values[lower] * (1 - weight) + values[upper] * weight


def _filter_event_types(events: list[Event], event_types: tuple[str, ...]) -> list[Event]:
    if not event_types:
        return events
    return [
        event
        for event in events
        if any(_event_type_matches(event.action, item) for item in event_types)
    ]


def _event_type_matches(action: str, pattern: str) -> bool:
    if pattern.endswith(".*"):
        return action.startswith(pattern[:-1])
    return action == pattern


def _matches_target(event: Event, target: ContainerEventsBatchTarget) -> bool:
    if target.container_id is not None and _event_container_id(event) != target.container_id:
        return False
    if target.task_id is not None and _event_task_id(event) != target.task_id:
        return False
    return not (target.stub_id is not None and _event_stub_id(event) != target.stub_id)


def _event_container_id(event: Event) -> str | None:
    value = event.data.get("container_id")
    if isinstance(value, str) and value:
        return value
    if event.resource_type in {"container", "sandbox"}:
        return event.resource_id
    return None


def _event_task_id(event: Event) -> str | None:
    value = event.data.get("task_id")
    if isinstance(value, str) and value:
        return value
    if event.resource_type == "task":
        return event.resource_id
    return None


def _event_stub_id(event: Event) -> str | None:
    value = event.data.get("stub_id")
    return value if isinstance(value, str) and value else None


def _metric_key(event_id: str) -> str:
    return f"{event_id.replace('.', '_')}_ms"


def _coverage_status(value: float) -> CoverageStatus:
    if value >= 1:
        return CoverageStatus.Full
    if value <= 0:
        return CoverageStatus.Missing
    return CoverageStatus.Partial


def _dedupe_trimmed(values: Iterable[str | None]) -> tuple[str, ...]:
    seen: set[str] = set()
    output: list[str] = []
    for value in values:
        if value is None:
            continue
        stripped = value.strip()
        if not stripped or stripped in seen:
            continue
        seen.add(stripped)
        output.append(stripped)
    return tuple(output)


def _first_value(values: Iterable[str | None]) -> str | None:
    for value in values:
        if value:
            return value
    return None
