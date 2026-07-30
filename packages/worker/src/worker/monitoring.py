from __future__ import annotations

import queue
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from time import monotonic
from typing import Protocol

from pydantic import Field, field_validator
from shared.contracts import ContractModel
from shared.realtime.contracts import CloudEventRecord, ContainerMetricsData
from shared.timestamps import utc_now

from worker.container_metrics import (
    ContainerMetricsCounterState,
    ContainerMetricsSourceFactory,
    WorkerContainerMetricsService,
)
from worker.events import ContainerLifecyclePayload, ContainerRequestContext, WorkerUsageEvidence
from worker.supervision import WorkerUsageEmissionResult


class ContainerRuntimeMonitorHandle(Protocol):
    def stop(self) -> ContainerRuntimeMonitoringResult: ...


class ContainerRuntimeMonitor(Protocol):
    def start_monitoring(
        self,
        request: ContainerRequestContext,
        *,
        started_pid: int,
    ) -> ContainerRuntimeMonitorHandle: ...


class WorkerUsageWindowRecorder(Protocol):
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
    ) -> WorkerUsageEmissionResult: ...


class ContainerLifecycleSink(Protocol):
    def publish_container_lifecycle(
        self,
        payload: ContainerLifecyclePayload,
    ) -> CloudEventRecord | None: ...


class ContainerRuntimeMonitoringResult(ContractModel):
    container_id: str
    started_pid: int
    duration_ms: int = 0
    metrics_samples: int = 0
    metrics_published: int = 0
    usage: WorkerUsageEmissionResult | None = None
    errors: list[str] = Field(default_factory=list)


@dataclass(frozen=True, slots=True)
class _UsageWindow:
    start_ms: int
    end_ms: int

    @property
    def duration_ms(self) -> int:
        return self.end_ms - self.start_ms


class ContainerRuntimeMonitorSettings(ContractModel):
    sample_interval_seconds: float = 5.0
    join_timeout_seconds: float = 2.0

    @field_validator("sample_interval_seconds", "join_timeout_seconds")
    @classmethod
    def positive_seconds(cls, value: float) -> float:
        if value <= 0:
            msg = "monitor timing values must be positive"
            raise ValueError(msg)
        return value


@dataclass(slots=True)
class WorkerContainerRuntimeMonitor:
    metrics: WorkerContainerMetricsService | None = None
    metrics_source_factory: ContainerMetricsSourceFactory | None = None
    usage_recorder: WorkerUsageWindowRecorder | None = None
    settings: ContainerRuntimeMonitorSettings = field(
        default_factory=ContainerRuntimeMonitorSettings
    )

    def start_monitoring(
        self,
        request: ContainerRequestContext,
        *,
        started_pid: int,
    ) -> ContainerRuntimeMonitorHandle:
        source = (
            self.metrics_source_factory.metrics_source_for_pid(started_pid)
            if self.metrics is not None and self.metrics_source_factory is not None
            else None
        )
        metrics = (
            WorkerContainerMetricsService(
                worker_id=self.metrics.worker_id,
                sink=self.metrics.sink,
                source=source,
            )
            if self.metrics is not None and source is not None
            else None
        )
        started_at = monotonic()
        handle = _ThreadedContainerRuntimeMonitorHandle(
            request=request,
            started_pid=started_pid,
            metrics=metrics,
            usage_recorder=self.usage_recorder,
            settings=self.settings,
            _started_at=started_at,
            _started_at_utc=utc_now(),
        )
        handle.start()
        return handle


@dataclass(slots=True)
class AsyncContainerLifecycleSink:
    sink: ContainerLifecycleSink
    max_queue_size: int = 1024
    errors: list[str] = field(default_factory=list)
    _queue: queue.Queue[ContainerLifecyclePayload] = field(init=False, repr=False)
    _thread: threading.Thread | None = field(default=None, init=False, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)

    def __post_init__(self) -> None:
        self._queue = queue.Queue(maxsize=max(self.max_queue_size, 1))

    def publish_container_lifecycle(
        self,
        payload: ContainerLifecyclePayload,
    ) -> None:
        self._ensure_started()
        try:
            self._queue.put_nowait(payload)
        except queue.Full:
            self._record_error(RuntimeError("container lifecycle queue is full"))

    def flush(self, *, timeout_seconds: float = 2.0) -> bool:
        deadline = monotonic() + max(timeout_seconds, 0)
        while self._queue.unfinished_tasks > 0:
            if monotonic() >= deadline:
                return False
            threading.Event().wait(min(0.01, max(deadline - monotonic(), 0)))
        return True

    def _ensure_started(self) -> None:
        if self._thread is not None:
            return
        with self._lock:
            if self._thread is not None:
                return
            self._thread = threading.Thread(
                target=self._run,
                name="container-lifecycle-publisher",
                daemon=True,
            )
            self._thread.start()

    def _run(self) -> None:
        while True:
            payload = self._queue.get()
            try:
                self.sink.publish_container_lifecycle(payload)
            except Exception as exc:  # pragma: no cover - defensive telemetry path
                self._record_error(exc)
            finally:
                self._queue.task_done()

    def _record_error(self, exc: Exception) -> None:
        with self._lock:
            self.errors.append(f"{type(exc).__name__}: {exc}")


@dataclass(slots=True)
class _ThreadedContainerRuntimeMonitorHandle:
    request: ContainerRequestContext
    started_pid: int
    metrics: WorkerContainerMetricsService | None
    usage_recorder: WorkerUsageWindowRecorder | None
    settings: ContainerRuntimeMonitorSettings
    _started_at: float
    _started_at_utc: datetime
    _stop: threading.Event = field(default_factory=threading.Event)
    _lock: threading.Lock = field(default_factory=threading.Lock)
    _usage_cursor_ms: int = 0
    _pending_usage_window: _UsageWindow | None = None
    _pending_usage_evidence: WorkerUsageEvidence = field(default_factory=WorkerUsageEvidence)
    _previous: ContainerMetricsCounterState | None = None
    _last_sample_at: float | None = None
    _samples: int = 0
    _published: int = 0
    _errors: list[str] = field(default_factory=list)
    _thread: threading.Thread | None = None

    def start(self) -> None:
        if self.metrics is None and self.usage_recorder is None:
            return
        self._thread = threading.Thread(
            target=self._run,
            name=f"container-monitor-{self.request.container_id}",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> ContainerRuntimeMonitoringResult:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=self.settings.join_timeout_seconds)
        current = monotonic()
        if self.metrics is not None:
            self._publish_once(recorded_at=current)
        duration_ms = max(1, int((current - self._started_at) * 1000))
        usage = self._record_usage_until(recorded_at=current)
        return ContainerRuntimeMonitoringResult(
            container_id=self.request.container_id,
            started_pid=self.started_pid,
            duration_ms=duration_ms,
            metrics_samples=self._samples,
            metrics_published=self._published,
            usage=usage,
            errors=list(self._errors),
        )

    def _run(self) -> None:
        self._publish_once(recorded_at=monotonic())
        while not self._stop.wait(self.settings.sample_interval_seconds):
            current = monotonic()
            self._publish_once(recorded_at=current)
            self._record_usage_until(recorded_at=current)

    def _publish_once(self, *, recorded_at: float) -> None:
        if self.metrics is None:
            return
        previous_sample_at = self._last_sample_at
        sample_interval_ms = max(
            int(
                (recorded_at - previous_sample_at) * 1_000
                if previous_sample_at is not None
                else self.settings.sample_interval_seconds * 1_000
            ),
            1,
        )
        try:
            result = self.metrics.sample_and_publish(
                self.request,
                previous=self._previous,
                sample_interval_ms=sample_interval_ms,
            )
        except Exception as exc:  # pragma: no cover - defensive worker boundary
            self._record_error(exc)
            return
        with self._lock:
            self._samples += 1
            if result.published:
                self._published += 1
            self._previous = result.next_state
            self._last_sample_at = recorded_at
            if result.payload is not None:
                self._pending_usage_evidence = self._pending_usage_evidence.plus(
                    _usage_evidence_from_metrics(result.payload.metrics)
                )

    def _record_error(self, exc: Exception) -> None:
        with self._lock:
            self._errors.append(f"{type(exc).__name__}: {exc}")

    def _record_usage_until(self, *, recorded_at: float) -> WorkerUsageEmissionResult | None:
        if self.usage_recorder is None:
            return None
        window = self._pending_usage_window or self._usage_window_for(recorded_at)
        if window.duration_ms <= 0:
            return None
        self._pending_usage_window = window
        try:
            usage = self.usage_recorder.record_usage_window(
                self.request,
                duration_ms=window.duration_ms,
                window_start_ms=window.start_ms,
                window_end_ms=window.end_ms,
                metering_window_started_at=self._started_at_utc
                + timedelta(milliseconds=window.start_ms),
                metering_window_ended_at=self._started_at_utc
                + timedelta(milliseconds=window.end_ms),
                evidence=self._pending_usage_evidence,
            )
        except Exception as exc:  # pragma: no cover - defensive worker boundary
            self._record_error(exc)
            return None
        self._usage_cursor_ms = window.end_ms
        self._pending_usage_window = None
        self._pending_usage_evidence = WorkerUsageEvidence()
        return usage

    def _usage_window_for(self, recorded_at: float) -> _UsageWindow:
        end_ms = max(
            self._usage_cursor_ms + 1,
            int((recorded_at - self._started_at) * 1000),
        )
        return _UsageWindow(start_ms=self._usage_cursor_ms, end_ms=end_ms)


def _usage_evidence_from_metrics(metrics: ContainerMetricsData) -> WorkerUsageEvidence:
    interval_seconds = metrics.sample_interval_ms / 1_000
    return WorkerUsageEvidence(
        cpu_used_core_seconds=metrics.cpu_used / 1_000 * interval_seconds,
        memory_rss_byte_seconds=metrics.memory_rss_bytes * interval_seconds,
        memory_swap_byte_seconds=metrics.memory_swap_bytes * interval_seconds,
        disk_used_byte_seconds=metrics.disk_used_bytes * interval_seconds,
        gpu_memory_byte_seconds=metrics.gpu_memory_used_bytes * interval_seconds,
        network_ingress_bytes=metrics.network_recv_bytes,
        network_egress_bytes=metrics.network_sent_bytes,
        network_ingress_packets=metrics.network_recv_packets,
        network_egress_packets=metrics.network_sent_packets,
        disk_read_bytes=metrics.disk_read_bytes,
        disk_write_bytes=metrics.disk_write_bytes,
    )
