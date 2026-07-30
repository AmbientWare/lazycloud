from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

from shared.realtime.contracts import ContainerMetricsPayload
from worker.container_metrics import (
    ContainerMetricsRawSample,
    WorkerContainerMetricsService,
)
from worker.events import (
    ContainerRequestContext,
    GpuMemoryCounters,
    WorkerPoolMode,
    WorkerUsageEvidence,
    WorkerUsageMetricName,
    WorkerUsageMetricPlan,
    plan_worker_usage_metrics,
)
from worker.monitoring import ContainerRuntimeMonitorSettings, WorkerContainerRuntimeMonitor
from worker.supervision import WorkerUsageEmissionResult
from worker.tools import NetworkIoCounters, ProcessIoCounters


@dataclass(slots=True)
class MetricsSink:
    payloads: list[ContainerMetricsPayload] = field(default_factory=list)

    def publish_container_metrics(self, payload: ContainerMetricsPayload) -> None:
        self.payloads.append(payload)


@dataclass(slots=True)
class SequenceMetricsSource:
    samples: list[ContainerMetricsRawSample]

    def sample(self, request: ContainerRequestContext) -> ContainerMetricsRawSample:
        _ = request
        if len(self.samples) == 1:
            return self.samples[0]
        return self.samples.pop(0)


@dataclass(slots=True)
class SequenceMetricsSourceFactory:
    source: SequenceMetricsSource
    pids: list[int] = field(default_factory=list)

    def metrics_source_for_pid(self, pid: int) -> SequenceMetricsSource:
        self.pids.append(pid)
        return self.source


@dataclass(slots=True)
class ConstantDiskUsage:
    """Reports the bytes a container's own layer occupies, as the quota does."""

    used_bytes_by_container: int

    def used_bytes(self, container_id: str) -> int:
        _ = container_id
        return self.used_bytes_by_container


@dataclass(slots=True)
class UsageRecorder:
    durations: list[int] = field(default_factory=list)
    windows: list[tuple[int, int]] = field(default_factory=list)
    metering_windows: list[tuple[datetime, datetime]] = field(default_factory=list)
    evidence: list[WorkerUsageEvidence] = field(default_factory=list)

    def record_usage_window(
        self,
        request: ContainerRequestContext,
        *,
        duration_ms: int,
        cost_per_ms: float | None = None,
        window_start_ms: int = 0,
        window_end_ms: int | None = None,
        metering_window_started_at: datetime,
        metering_window_ended_at: datetime,
        evidence: WorkerUsageEvidence | None = None,
    ) -> WorkerUsageEmissionResult:
        _ = cost_per_ms
        self.durations.append(duration_ms)
        end_ms = window_start_ms + duration_ms if window_end_ms is None else window_end_ms
        self.windows.append((window_start_ms, end_ms))
        self.metering_windows.append((metering_window_started_at, metering_window_ended_at))
        self.evidence.append(evidence or WorkerUsageEvidence())
        return WorkerUsageEmissionResult(
            worker_id="worker-1",
            container_id=request.container_id,
            duration_ms=duration_ms,
            window_start_ms=window_start_ms,
            window_end_ms=end_ms,
            metering_window_started_at=metering_window_started_at,
            metering_window_ended_at=metering_window_ended_at,
            pool_mode=WorkerPoolMode.Public,
            reason="worker usage emitted",
        )


def test_worker_container_metrics_service_computes_deltas_and_publishes() -> None:
    sink = MetricsSink()
    service = WorkerContainerMetricsService(worker_id="worker-1", sink=sink)
    request = ContainerRequestContext(
        container_id="ctr-1",
        workspace_id="workspace-1",
        stub_id="stub-1",
        app_id="app-1",
        cpu_millicores=2000,
        memory_mib=512,
        gpu="L4",
        gpu_count=1,
    )
    previous = ContainerMetricsRawSample(
        process_io=ProcessIoCounters(disk_read_bytes=100, disk_write_bytes=50),
        network_interfaces=[
            NetworkIoCounters(name="lo", bytes_recv=1000, bytes_sent=1000),
            NetworkIoCounters(name="eth0", bytes_recv=200, bytes_sent=500, packets_recv=2),
        ],
    ).counter_state()
    current = ContainerMetricsRawSample(
        cpu_used_millicores=1000,
        memory_rss_bytes=128 * 1024 * 1024,
        memory_vms_bytes=256 * 1024 * 1024,
        memory_swap_bytes=10,
        process_io=ProcessIoCounters(disk_read_bytes=80, disk_write_bytes=90),
        network_interfaces=[
            NetworkIoCounters(name="lo", bytes_recv=5000, bytes_sent=5000),
            NetworkIoCounters(
                name="eth0",
                bytes_recv=260,
                bytes_sent=550,
                packets_recv=5,
                packets_sent=7,
            ),
        ],
        gpu_memory=GpuMemoryCounters(used_bytes=20, total_bytes=40),
    )

    result = service.publish_sample(
        request,
        current,
        previous=previous,
        sample_interval_ms=1000,
    )

    assert result.published
    assert sink.payloads == [result.payload]
    assert result.payload is not None
    metrics = result.payload.metrics
    assert metrics.cpu_pct == 50
    assert metrics.memory_total_bytes == 512 * 1024 * 1024
    assert metrics.disk_read_bytes == 0
    assert metrics.disk_write_bytes == 40
    assert metrics.network_recv_bytes == 60
    assert metrics.network_sent_bytes == 50
    assert metrics.network_recv_packets == 3
    assert metrics.network_sent_packets == 7
    assert metrics.gpu_memory_used_bytes == 20
    assert result.next_state.process_io.disk_write_bytes == 90
    assert result.next_state.network_io.bytes_recv == 260


def test_worker_container_runtime_monitor_publishes_metrics_and_usage_on_stop() -> None:
    sink = MetricsSink()
    source = SequenceMetricsSource(
        samples=[
            ContainerMetricsRawSample(
                process_io=ProcessIoCounters(disk_read_bytes=1),
                network_interfaces=[NetworkIoCounters(name="eth0", bytes_recv=1)],
            ),
            ContainerMetricsRawSample(
                cpu_used_millicores=500,
                process_io=ProcessIoCounters(disk_read_bytes=5),
                network_interfaces=[NetworkIoCounters(name="eth0", bytes_recv=9)],
            ),
        ]
    )
    source_factory = SequenceMetricsSourceFactory(source)
    usage = UsageRecorder()
    monitor = WorkerContainerRuntimeMonitor(
        metrics=WorkerContainerMetricsService(
            worker_id="worker-1",
            sink=sink,
            disk_usage=ConstantDiskUsage(used_bytes_by_container=64 * 1024**2),
        ),
        metrics_source_factory=source_factory,
        usage_recorder=usage,
        settings=ContainerRuntimeMonitorSettings(sample_interval_seconds=60),
    )
    request = ContainerRequestContext(
        container_id="ctr-1",
        workspace_id="workspace-1",
        stub_id="stub-1",
        cpu_millicores=1000,
        memory_mib=128,
    )

    handle = monitor.start_monitoring(request, started_pid=123)
    result = handle.stop()

    assert source_factory.pids == [123]
    assert result.metrics_samples >= 2
    assert result.metrics_published >= 1
    assert result.usage is not None
    assert usage.durations == [result.duration_ms]
    assert usage.windows == [(0, result.duration_ms)]
    [(metering_started_at, metering_ended_at)] = usage.metering_windows
    assert metering_started_at.tzinfo is UTC
    assert metering_ended_at.tzinfo is UTC
    assert int((metering_ended_at - metering_started_at).total_seconds() * 1000) == (
        result.duration_ms
    )
    assert usage.evidence[0].disk_read_bytes == 4
    assert usage.evidence[0].network_ingress_bytes == 8
    assert usage.evidence[0].cpu_used_core_seconds > 0
    # Disk occupancy has to survive both the per-container rebind of the metrics
    # service and the accumulation into the window, or it is billed as zero.
    assert usage.evidence[0].disk_used_byte_seconds > 0
    assert sink.payloads
    assert sink.payloads[-1].container_id == "ctr-1"
    assert sink.payloads[-1].metrics.disk_read_bytes == 4
    assert sink.payloads[-1].metrics.disk_used_bytes == 64 * 1024**2


def test_worker_container_metrics_service_primes_without_publishing_first_sample() -> None:
    sink = MetricsSink()
    service = WorkerContainerMetricsService(worker_id="worker-1", sink=sink)
    request = ContainerRequestContext(container_id="ctr-1")
    sample = ContainerMetricsRawSample(
        process_io=ProcessIoCounters(disk_read_bytes=100),
        network_interfaces=[NetworkIoCounters(name="eth0", bytes_recv=100)],
    )

    result = service.publish_sample(
        request,
        sample,
        previous=None,
        sample_interval_ms=1000,
    )

    assert not result.published
    assert result.payload is None
    assert result.reason == "metrics counter state primed"
    assert sink.payloads == []
    assert result.next_state.process_io.disk_read_bytes == 100
    assert result.next_state.network_io.bytes_recv == 100


def test_billing_takes_the_greater_of_reservation_and_measured_usage() -> None:
    """A request is a floor, so a bursting container must not bill as if capped."""
    request = ContainerRequestContext(
        container_id="ctr-1",
        workspace_id="ws-1",
        cpu_millicores=125,
        memory_mib=128,
    )

    def value_of(
        plans: tuple[WorkerUsageMetricPlan, ...],
        name: WorkerUsageMetricName,
    ) -> float:
        for plan in plans:
            if plan.name is name:
                return plan.value
        raise AssertionError(f"{name} was not emitted")

    # Idle: the reservation is the floor.
    idle = plan_worker_usage_metrics(
        worker_id="w",
        request=request,
        duration_ms=10_000,
        evidence=WorkerUsageEvidence(cpu_used_core_seconds=0.1),
    )
    assert value_of(idle, WorkerUsageMetricName.Cpu) == 1.25

    # Bursting past the request bills the usage, not the reservation.
    bursting = plan_worker_usage_metrics(
        worker_id="w",
        request=request,
        duration_ms=10_000,
        evidence=WorkerUsageEvidence(
            cpu_used_core_seconds=80.0,
            memory_rss_byte_seconds=8 * 1024**3,
        ),
    )
    assert value_of(bursting, WorkerUsageMetricName.Cpu) == 80.0
    assert value_of(bursting, WorkerUsageMetricName.Memory) == 8.0


def test_ephemeral_disk_bills_what_was_used_not_the_oversubscribed_ceiling() -> None:
    """The disk cap is oversubscribed by design, so only real occupancy is billable."""
    request = ContainerRequestContext(
        container_id="ctr-1",
        workspace_id="ws-1",
        cpu_millicores=125,
        memory_mib=128,
        disk_limit_bytes=100 * 1024**3,
    )
    plans = plan_worker_usage_metrics(
        worker_id="w",
        request=request,
        duration_ms=10_000,
        evidence=WorkerUsageEvidence(disk_used_byte_seconds=5 * 1024**3),
    )
    emitted = {plan.name: plan.value for plan in plans}
    assert emitted[WorkerUsageMetricName.ContainerDisk] == float(5 * 1024**3)

    # No occupancy, nothing billed: the ceiling alone is never charged.
    idle = plan_worker_usage_metrics(
        worker_id="w", request=request, duration_ms=10_000, evidence=WorkerUsageEvidence()
    )
    assert WorkerUsageMetricName.ContainerDisk not in {plan.name for plan in idle}
