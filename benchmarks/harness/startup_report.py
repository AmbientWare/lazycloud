from __future__ import annotations

from collections.abc import Iterable
from enum import StrEnum

from pydantic import Field, field_validator

from benchmarks.harness.models import BenchmarkModel


class StartupClientTiming(StrEnum):
    Accepted = "accepted_ms"
    RunningObserved = "running_observed_ms"
    ProcessReady = "process_ready_ms"
    Interactive = "exec_complete_ms"


class StartupPhaseId(StrEnum):
    Scheduler = "scheduler"
    SchedulerBacklog = "scheduler.backlog"
    SchedulerWorkerSelection = "scheduler.worker_selection"
    WorkerQueue = "worker.queue"
    WorkerReceiveToRunning = "worker.receive_to_running"
    ImageLoad = "image.load"
    ImageRegistryPull = "image.registry_pull"
    ImageLayerMount = "image.layer_mount"
    ImageCacheRestore = "image.cache_restore"
    MountSetup = "mount.setup"
    NetworkSetup = "network.setup"
    ProcessStartup = "process.startup"
    ProcessManagerReady = "process_manager.ready"
    RunnerProcessStarted = "runner.process_started"
    RunnerImport = "runner.import"
    RunnerExecution = "runner.execution"
    ResultDelivery = "result.delivery"


class StartupVerdict(StrEnum):
    Passed = "passed"
    Warning = "warning"
    Failed = "failed"


class StartupPhaseDefinition(BenchmarkModel):
    phase: StartupPhaseId
    label: str
    required: bool = False
    aggregate: bool = False


STARTUP_PHASES: tuple[StartupPhaseDefinition, ...] = (
    StartupPhaseDefinition(
        phase=StartupPhaseId.Scheduler,
        label="Scheduler total",
        required=True,
        aggregate=True,
    ),
    StartupPhaseDefinition(phase=StartupPhaseId.SchedulerBacklog, label="Scheduler backlog"),
    StartupPhaseDefinition(
        phase=StartupPhaseId.SchedulerWorkerSelection,
        label="Worker selection",
    ),
    StartupPhaseDefinition(phase=StartupPhaseId.WorkerQueue, label="Worker queue"),
    StartupPhaseDefinition(
        phase=StartupPhaseId.WorkerReceiveToRunning,
        label="Worker receive to running",
        required=True,
    ),
    StartupPhaseDefinition(phase=StartupPhaseId.ImageLoad, label="Image load", required=True),
    StartupPhaseDefinition(phase=StartupPhaseId.ImageRegistryPull, label="Image registry pull"),
    StartupPhaseDefinition(phase=StartupPhaseId.ImageLayerMount, label="Image layer mount"),
    StartupPhaseDefinition(phase=StartupPhaseId.ImageCacheRestore, label="Image cache restore"),
    StartupPhaseDefinition(phase=StartupPhaseId.MountSetup, label="Mount setup"),
    StartupPhaseDefinition(phase=StartupPhaseId.NetworkSetup, label="Network setup"),
    StartupPhaseDefinition(
        phase=StartupPhaseId.ProcessStartup, label="Process startup", required=True
    ),
    StartupPhaseDefinition(
        phase=StartupPhaseId.ProcessManagerReady,
        label="Process manager ready",
    ),
    StartupPhaseDefinition(phase=StartupPhaseId.RunnerProcessStarted, label="Runner process"),
    StartupPhaseDefinition(phase=StartupPhaseId.RunnerImport, label="Runner import"),
    StartupPhaseDefinition(phase=StartupPhaseId.RunnerExecution, label="Runner execution"),
    StartupPhaseDefinition(phase=StartupPhaseId.ResultDelivery, label="Result delivery"),
)

PHASE_DEFINITIONS: dict[StartupPhaseId, StartupPhaseDefinition] = {
    phase.phase: phase for phase in STARTUP_PHASES
}


class LatencySummary(BenchmarkModel):
    count: int
    min: float
    p50: float
    p90: float
    p95: float
    p99: float
    max: float


class StartupSample(BenchmarkModel):
    index: int
    sandbox_id: str | None = None
    task_id: str | None = None
    warmup: bool = False
    ok: bool = True
    accepted_ms: float | None = None
    running_observed_ms: float | None = None
    process_ready_ms: float | None = None
    exec_complete_ms: float | None = None
    exec_exit_code: int | None = None
    exec_verified: bool = True
    error: str | None = None
    phase_durations_ms: dict[StartupPhaseId, float] = Field(default_factory=dict)

    @field_validator(
        "accepted_ms",
        "running_observed_ms",
        "process_ready_ms",
        "exec_complete_ms",
    )
    @classmethod
    def _non_negative_timing(cls, value: float | None) -> float | None:
        if value is not None and value < 0:
            msg = "startup timings must be non-negative"
            raise ValueError(msg)
        return value


class StartupPhaseSummary(BenchmarkModel):
    phase: StartupPhaseId
    label: str
    required: bool = False
    summary: LatencySummary


class StartupBottleneck(BenchmarkModel):
    phase: StartupPhaseId
    label: str
    p95_ms: float
    count: int


class StartupEventCoverage(BenchmarkModel):
    samples: int
    samples_with_events: int
    required_present: int
    required_total: int
    missing_required: tuple[StartupPhaseId, ...] = ()


class StartupReport(BenchmarkModel):
    samples: int
    measured_samples: int
    interactive_samples: int
    client_timings: dict[StartupClientTiming, LatencySummary] = Field(default_factory=dict)
    phase_summaries: tuple[StartupPhaseSummary, ...] = ()
    primary_bottleneck: StartupBottleneck | None = None
    event_coverage: StartupEventCoverage
    verdict: StartupVerdict


def percentile(values: Iterable[float], percentile_value: float) -> float | None:
    clean = sorted(float(value) for value in values)
    if not clean:
        return None
    if len(clean) == 1:
        return clean[0]
    rank = (len(clean) - 1) * (percentile_value / 100)
    lower = int(rank)
    upper = min(lower + 1, len(clean) - 1)
    weight = rank - lower
    return clean[lower] * (1 - weight) + clean[upper] * weight


def summarize_values(values: Iterable[float | None]) -> LatencySummary | None:
    clean = [float(value) for value in values if value is not None]
    if not clean:
        return None
    return LatencySummary(
        count=len(clean),
        min=min(clean),
        p50=percentile(clean, 50) or 0,
        p90=percentile(clean, 90) or 0,
        p95=percentile(clean, 95) or 0,
        p99=percentile(clean, 99) or 0,
        max=max(clean),
    )


def measured_samples(samples: Iterable[StartupSample]) -> list[StartupSample]:
    return [sample for sample in samples if not sample.warmup]


def interactive_samples(samples: Iterable[StartupSample]) -> list[StartupSample]:
    return [
        sample
        for sample in samples
        if sample.ok
        and sample.exec_complete_ms is not None
        and sample.exec_exit_code in (None, 0)
        and sample.exec_verified
    ]


def build_startup_report(
    samples: Iterable[StartupSample],
    *,
    bottleneck_warning_ms: float = 5_000,
) -> StartupReport:
    sample_list = list(samples)
    measured = measured_samples(sample_list)
    interactive = interactive_samples(measured)
    client_timings = _summarize_client_timings(measured)
    phase_summaries = _summarize_phases(measured)
    bottleneck = primary_bottleneck(phase_summaries)
    coverage = event_coverage(measured)
    verdict = _verdict(
        measured_count=len(measured),
        interactive_count=len(interactive),
        coverage=coverage,
        bottleneck=bottleneck,
        bottleneck_warning_ms=bottleneck_warning_ms,
    )
    return StartupReport(
        samples=len(sample_list),
        measured_samples=len(measured),
        interactive_samples=len(interactive),
        client_timings=client_timings,
        phase_summaries=tuple(phase_summaries),
        primary_bottleneck=bottleneck,
        event_coverage=coverage,
        verdict=verdict,
    )


def primary_bottleneck(
    phase_summaries: Iterable[StartupPhaseSummary],
) -> StartupBottleneck | None:
    candidates = [summary for summary in phase_summaries if summary.summary.count > 0]
    if not candidates:
        return None
    slowest = max(candidates, key=lambda item: item.summary.p95)
    return StartupBottleneck(
        phase=slowest.phase,
        label=slowest.label,
        p95_ms=slowest.summary.p95,
        count=slowest.summary.count,
    )


def event_coverage(samples: Iterable[StartupSample]) -> StartupEventCoverage:
    sample_list = list(samples)
    required = tuple(phase.phase for phase in STARTUP_PHASES if phase.required)
    present = {
        phase
        for sample in sample_list
        for phase, value in sample.phase_durations_ms.items()
        if phase in required and value is not None
    }
    samples_with_events = sum(1 for sample in sample_list if sample.phase_durations_ms)
    missing = tuple(phase for phase in required if phase not in present)
    return StartupEventCoverage(
        samples=len(sample_list),
        samples_with_events=samples_with_events,
        required_present=len(present),
        required_total=len(required),
        missing_required=missing,
    )


def render_startup_markdown(report: StartupReport) -> str:
    lines = [
        "# Startup Report",
        "",
        f"- Verdict: `{report.verdict.value}`",
        (
            f"- Samples: `{report.measured_samples}` measured, "
            f"`{report.interactive_samples}` interactive"
        ),
    ]
    if report.primary_bottleneck is not None:
        lines.append(
            "- Primary bottleneck: "
            f"`{report.primary_bottleneck.phase.value}` "
            f"p95={report.primary_bottleneck.p95_ms:.2f}ms"
        )
    lines.extend(
        [
            "",
            "## Client Timings",
            "",
            "| Timing | Count | p50 ms | p95 ms | max ms |",
            "| --- | ---: | ---: | ---: | ---: |",
        ]
    )
    for timing, summary in report.client_timings.items():
        lines.append(
            f"| {timing.value} | {summary.count} | {summary.p50:.2f} | "
            f"{summary.p95:.2f} | {summary.max:.2f} |"
        )
    lines.extend(
        [
            "",
            "## Server Phases",
            "",
            "| Phase | Count | p50 ms | p95 ms | required |",
            "| --- | ---: | ---: | ---: | --- |",
        ]
    )
    for phase in report.phase_summaries:
        lines.append(
            f"| {phase.label} | {phase.summary.count} | {phase.summary.p50:.2f} | "
            f"{phase.summary.p95:.2f} | `{str(phase.required).lower()}` |"
        )
    lines.extend(
        [
            "",
            "## Event Coverage",
            "",
            f"- Samples with events: `{report.event_coverage.samples_with_events}`",
            "- Required phases: "
            f"`{report.event_coverage.required_present}/{report.event_coverage.required_total}`",
        ]
    )
    if report.event_coverage.missing_required:
        missing = ", ".join(phase.value for phase in report.event_coverage.missing_required)
        lines.append(f"- Missing required phases: `{missing}`")
    return "\n".join(lines) + "\n"


def _summarize_client_timings(
    samples: Iterable[StartupSample],
) -> dict[StartupClientTiming, LatencySummary]:
    summary: dict[StartupClientTiming, LatencySummary] = {}
    sample_list = list(samples)
    for timing in StartupClientTiming:
        values = [getattr(sample, timing.value) for sample in sample_list]
        if timing_summary := summarize_values(values):
            summary[timing] = timing_summary
    return summary


def _summarize_phases(samples: Iterable[StartupSample]) -> list[StartupPhaseSummary]:
    sample_list = list(samples)
    summaries: list[StartupPhaseSummary] = []
    for definition in STARTUP_PHASES:
        values = [sample.phase_durations_ms.get(definition.phase) for sample in sample_list]
        if phase_summary := summarize_values(values):
            summaries.append(
                StartupPhaseSummary(
                    phase=definition.phase,
                    label=definition.label,
                    required=definition.required,
                    summary=phase_summary,
                )
            )
    return summaries


def _verdict(
    *,
    measured_count: int,
    interactive_count: int,
    coverage: StartupEventCoverage,
    bottleneck: StartupBottleneck | None,
    bottleneck_warning_ms: float,
) -> StartupVerdict:
    if measured_count == 0 or interactive_count < measured_count:
        return StartupVerdict.Failed
    if coverage.missing_required:
        return StartupVerdict.Warning
    if bottleneck is not None and bottleneck.p95_ms > bottleneck_warning_ms:
        return StartupVerdict.Warning
    return StartupVerdict.Passed
