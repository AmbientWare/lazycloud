from __future__ import annotations

import logging
import queue
import threading
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from time import monotonic, sleep
from typing import Protocol

from pydantic import field_validator
from shared.contracts import ContractModel
from shared.realtime.contracts import CloudEventRecord, ContainerMetricsData
from shared.scheduling import (
    SchedulerContainerStatus,
    WorkerContainerState,
)
from shared.timestamps import utc_now

from worker.container_metrics import (
    ContainerMetricsCounterState,
    ContainerMetricsSourceFactory,
    WorkerContainerMetricsService,
)
from worker.events import ContainerLifecyclePayload, ContainerRequestContext, WorkerUsageEvidence
from worker.finalization import ContainerStatusUpdater
from worker.status import (
    CONTAINER_STATE_TTL_SECONDS,
    WorkerContainerStatus,
    WorkerStatusHeartbeatAction,
    WorkerStatusHeartbeatPlan,
    normalize_worker_container_status,
    plan_worker_status_heartbeat,
)
from worker.supervision import WorkerUsageEmissionResult

LOGGER = logging.getLogger(__name__)

_HEARTBEAT_REFRESHES_PER_TTL = 4
_HEARTBEAT_STATUSES = {
    WorkerContainerStatus.Pending: SchedulerContainerStatus.Pending,
    WorkerContainerStatus.Running: SchedulerContainerStatus.Running,
}


def _heartbeat_interval_seconds() -> float:
    """How often to re-arm, given how long the platform waits before reaping.

    Several refreshes inside one TTL, so a lost write or a late tick costs
    margin rather than the container.
    """

    return CONTAINER_STATE_TTL_SECONDS / _HEARTBEAT_REFRESHES_PER_TTL


def _heartbeat_next_status(plan: WorkerStatusHeartbeatPlan) -> SchedulerContainerStatus | None:
    """The status this heartbeat may assert, or nothing where it may assert none.

    A worker's liveness says something about a container the record calls
    pending or running, and nothing about one being stopped or already finished.
    Re-arming those would hold open a state something else has moved past.
    """

    if plan.action is not WorkerStatusHeartbeatAction.UpdateStatus:
        return None
    return _HEARTBEAT_STATUSES.get(plan.next_status)


class ContainerRuntimeMonitorHandle(Protocol):
    def runtime_started(self, pid: int) -> None: ...

    def stop(self) -> ContainerRuntimeMonitoringResult: ...


class ContainerStateHeartbeatRepository(ContainerStatusUpdater, Protocol):
    """The scheduler's view of a container, read and re-armed by its worker.

    The write is `ContainerStatusUpdater`, which every finalization path already
    speaks; the read is what makes this a heartbeat rather than a report, since
    durable recovery must restore a missing state before the worker refreshes it.
    """

    def get_container_state(self, container_id: str) -> WorkerContainerState | None: ...


class ContainerRuntimeMonitor(Protocol):
    def start_monitoring(
        self,
        request: ContainerRequestContext,
    ) -> ContainerRuntimeMonitorHandle: ...


class WorkerUsageWindowRecorder(Protocol):
    def record_usage_window(
        self,
        request: ContainerRequestContext,
        *,
        duration_ms: int,
        window_start_ms: int = 0,
        window_end_ms: int | None = None,
        metering_window_started_at: datetime,
        metering_window_ended_at: datetime,
        evidence: WorkerUsageEvidence | None = None,
        measurement_complete: bool = False,
    ) -> WorkerUsageEmissionResult: ...


class ContainerLifecycleSink(Protocol):
    def publish_container_lifecycle(
        self,
        payload: ContainerLifecyclePayload,
    ) -> CloudEventRecord | None: ...


class ContainerRuntimeMonitoringResult(ContractModel):
    container_id: str
    started_pid: int
    exited_at: datetime
    duration_ms: int = 0
    metrics_samples: int = 0
    metrics_published: int = 0
    usage: WorkerUsageEmissionResult | None = None


@dataclass(frozen=True, slots=True)
class _UsageWindow:
    start_ms: int
    end_ms: int
    measurement_complete: bool = True

    @property
    def duration_ms(self) -> int:
        return self.end_ms - self.start_ms


class ContainerRuntimeMonitorSettings(ContractModel):
    sample_interval_seconds: float = 5.0
    join_timeout_seconds: float = 2.0
    exit_flush_attempts: int = 3
    """Bound final usage retries so an unavailable API cannot hold shutdown open."""

    exit_flush_retry_seconds: float = 0.5

    @field_validator("sample_interval_seconds", "join_timeout_seconds", "exit_flush_retry_seconds")
    @classmethod
    def positive_seconds(cls, value: float) -> float:
        if value <= 0:
            msg = "monitor timing values must be positive"
            raise ValueError(msg)
        return value

    @field_validator("exit_flush_attempts")
    @classmethod
    def at_least_one_attempt(cls, value: int) -> int:
        if value < 1:
            msg = "the last drain of a container's life must be attempted at least once"
            raise ValueError(msg)
        return value


@dataclass(slots=True)
class WorkerContainerRuntimeMonitor:
    metrics: WorkerContainerMetricsService | None = None
    metrics_source_factory: ContainerMetricsSourceFactory | None = None
    usage_recorder: WorkerUsageWindowRecorder | None = None
    container_states: ContainerStateHeartbeatRepository | None = None
    settings: ContainerRuntimeMonitorSettings = field(
        default_factory=ContainerRuntimeMonitorSettings
    )

    def start_monitoring(
        self,
        request: ContainerRequestContext,
    ) -> ContainerRuntimeMonitorHandle:
        source = (
            self.metrics_source_factory.metrics_source_for_container(request.container_id)
            if self.metrics is not None and self.metrics_source_factory is not None
            else None
        )
        # Rebind the configured service to this container's source rather than
        # rebuilding it: a hand-listed field set silently omits the disk usage
        # reader, so occupancy reads as zero for every container.
        metrics = (
            replace(self.metrics, source=source)
            if self.metrics is not None and source is not None
            else None
        )
        started_at = monotonic()
        handle = _ThreadedContainerRuntimeMonitorHandle(
            request=request,
            metrics=metrics,
            usage_recorder=self.usage_recorder,
            container_states=self.container_states,
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
    metrics: WorkerContainerMetricsService | None
    usage_recorder: WorkerUsageWindowRecorder | None
    container_states: ContainerStateHeartbeatRepository | None
    settings: ContainerRuntimeMonitorSettings
    _started_at: float
    _started_at_utc: datetime
    started_pid: int = 0
    _stop: threading.Event = field(default_factory=threading.Event)
    _lock: threading.Lock = field(default_factory=threading.Lock)
    _sample_lock: threading.Lock = field(default_factory=threading.Lock)
    _usage_cursor_ms: int = 0
    _pending_usage_evidence: WorkerUsageEvidence = field(default_factory=WorkerUsageEvidence)
    _pending_measurement_complete: bool = True
    _held: list[tuple[_UsageWindow, WorkerUsageEvidence]] = field(default_factory=list)
    _previous: ContainerMetricsCounterState | None = None
    _last_sample_at: float | None = None
    _samples: int = 0
    _published: int = 0
    _thread: threading.Thread | None = None
    _heartbeat_stopped: bool = False
    _last_heartbeat_at: float = float("-inf")

    def runtime_started(self, pid: int) -> None:
        if pid <= 0:
            raise ValueError("runtime process ID must be positive")
        with self._lock:
            self.started_pid = pid
            self._last_heartbeat_at = float("-inf")

    def start(self) -> None:
        if self.metrics is None and self.usage_recorder is None and self.container_states is None:
            return
        self._publish_once(recorded_at=self._started_at)
        self._thread = threading.Thread(
            target=self._run,
            name=f"container-monitor-{self.request.container_id}",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> ContainerRuntimeMonitoringResult:
        current = monotonic()
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=self.settings.join_timeout_seconds)
        if self.metrics is not None:
            self._publish_once(recorded_at=current)
        duration_ms = max(1, int((current - self._started_at) * 1000))
        usage = self._flush_usage(recorded_at=current)
        return ContainerRuntimeMonitoringResult(
            container_id=self.request.container_id,
            started_pid=self.started_pid,
            exited_at=self._started_at_utc + timedelta(milliseconds=duration_ms),
            duration_ms=duration_ms,
            metrics_samples=self._samples,
            metrics_published=self._published,
            usage=usage,
        )

    def _run(self) -> None:
        while not self._stop.wait(self.settings.sample_interval_seconds):
            current = monotonic()
            self._heartbeat_container_state(recorded_at=current)
            self._publish_once(recorded_at=current)
            try:
                self._record_usage_until(recorded_at=current)
            except Exception:  # pragma: no cover - defensive worker boundary
                # Keep metering after a failed claim; this is the only sampler.
                LOGGER.warning(
                    "container usage claim failed",
                    exc_info=True,
                    extra={"container_id": self.request.container_id},
                )

    def _heartbeat_container_state(self, *, recorded_at: float) -> None:
        """Refresh live state on its lease interval, preserving durable recovery.

        Missing cache state is retried until worker admission restores it.
        Stopping or terminal state ends the heartbeat while metering continues.
        """

        if self.container_states is None or self._heartbeat_stopped:
            return
        if self.started_pid == 0:
            return
        if recorded_at - self._last_heartbeat_at < _heartbeat_interval_seconds():
            return
        container_id = self.request.container_id
        try:
            state = self.container_states.get_container_state(container_id)
            plan = plan_worker_status_heartbeat(
                state_status=(
                    normalize_worker_container_status(state.status) if state is not None else None
                ),
                runtime_started=True,
                runtime_pid=self.started_pid,
            )
            if plan.action is WorkerStatusHeartbeatAction.Error:
                return
            next_status = _heartbeat_next_status(plan)
            if next_status is None:
                self._heartbeat_stopped = True
                LOGGER.info(
                    "container state heartbeat stopping",
                    extra={"container_id": container_id, "reason": plan.reason},
                )
                return
            self._last_heartbeat_at = recorded_at
            self.container_states.update_container_status(
                container_id,
                next_status,
                ttl_seconds=plan.expiry_seconds,
            )
        except Exception:
            # One failed refresh is survivable — the TTL outlives many ticks, and
            # the next one re-arms it. Letting it out of this thread would end
            # metering for the rest of the container's life along with it.
            LOGGER.warning(
                "container state heartbeat failed",
                exc_info=True,
                extra={"container_id": container_id},
            )

    def _flush_usage(self, *, recorded_at: float) -> WorkerUsageEmissionResult | None:
        """Drain what the container owes for the last time, retrying if refused.

        The sample loop stops at the first refusal on purpose, because the next
        tick offers the window again. This is the tick after which there is no
        next one: the thread has stopped, `stop()` is called once, and anything
        still held when this returns is ground the platform is never told about.

        Retried rather than persisted, and a handful of times rather than
        forever, because a customer is waiting on this teardown. What exhaustion
        leaves is a log line naming the container and the windows — the only
        record available when the thing that would have stored one is the thing
        refusing.
        """

        emitted: WorkerUsageEmissionResult | None = None
        outstanding: list[_UsageWindow] = []
        for attempt in range(1, self.settings.exit_flush_attempts + 1):
            try:
                drained = self._record_usage_until(recorded_at=recorded_at)
            except Exception:  # pragma: no cover - defensive worker boundary
                LOGGER.warning(
                    "container usage claim failed during the final drain",
                    exc_info=True,
                    extra={"container_id": self.request.container_id},
                )
                drained = None
            if drained is not None:
                emitted = drained
            with self._lock:
                outstanding = [window for window, _ in self._held]
            if not outstanding:
                return emitted
            if attempt < self.settings.exit_flush_attempts:
                sleep(self.settings.exit_flush_retry_seconds)
        LOGGER.error(
            "container usage was lost: %d window(s) never reached the platform",
            len(outstanding),
            extra={
                "container_id": self.request.container_id,
                "workspace_id": self.request.workspace_id,
                "windows": [f"{window.start_ms}-{window.end_ms}ms" for window in outstanding],
            },
        )
        return emitted

    def _publish_once(self, *, recorded_at: float) -> None:
        # Shutdown may reach here while the sampler is still in external I/O.
        # Two reads must never claim a delta from the same previous counter.
        if not self._sample_lock.acquire(blocking=False):
            return
        try:
            self._publish_sample(recorded_at=recorded_at)
        finally:
            self._sample_lock.release()

    def _publish_sample(self, *, recorded_at: float) -> None:
        if self.metrics is None:
            with self._lock:
                self._pending_measurement_complete = False
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
        except Exception:  # pragma: no cover - defensive worker boundary
            with self._lock:
                self._pending_measurement_complete = False
                # The missing interval is billed at its reservation. A later
                # counter delta must not include that interval again.
                self._previous = None
                self._last_sample_at = None
            LOGGER.warning(
                "container metrics sample failed",
                exc_info=True,
                extra={"container_id": self.request.container_id},
            )
            return
        with self._lock:
            self._samples += 1
            if not result.measurement_complete:
                self._pending_measurement_complete = False
            if result.published:
                self._published += 1
            self._previous = result.next_state
            self._last_sample_at = recorded_at
            if result.payload is not None:
                self._pending_usage_evidence = self._pending_usage_evidence.plus(
                    _usage_evidence_from_metrics(result.payload.metrics).model_copy(
                        update={
                            "cpu_used_core_seconds": result.cpu_used_core_seconds,
                            "memory_rss_byte_seconds": result.memory_rss_byte_seconds,
                        }
                    )
                )
                self._pending_usage_evidence = self._pending_usage_evidence.plus(
                    WorkerUsageEvidence(network_egress_bytes=result.network_egress_bytes)
                )

    def _record_usage_until(self, *, recorded_at: float) -> WorkerUsageEmissionResult | None:
        """Retry unchanged windows before claiming new usage; stop on failure."""

        recorder = self.usage_recorder
        if recorder is None:
            return None
        emitted: WorkerUsageEmissionResult | None = None
        while (claimed := self._claim_usage_window(recorded_at)) is not None:
            window, evidence = claimed
            usage = self._emit_usage_window(recorder, window, evidence)
            if usage is None:
                break
            emitted = usage
        return emitted

    def _emit_usage_window(
        self,
        recorder: WorkerUsageWindowRecorder,
        window: _UsageWindow,
        evidence: WorkerUsageEvidence,
    ) -> WorkerUsageEmissionResult | None:
        try:
            return recorder.record_usage_window(
                self.request,
                duration_ms=window.duration_ms,
                window_start_ms=window.start_ms,
                window_end_ms=window.end_ms,
                metering_window_started_at=self._started_at_utc
                + timedelta(milliseconds=window.start_ms),
                metering_window_ended_at=self._started_at_utc
                + timedelta(milliseconds=window.end_ms),
                evidence=evidence,
                measurement_complete=window.measurement_complete,
            )
        except Exception:  # pragma: no cover - defensive worker boundary
            LOGGER.warning(
                "container usage window was not recorded",
                exc_info=True,
                extra={
                    "container_id": self.request.container_id,
                    "window_start_ms": window.start_ms,
                    "window_end_ms": window.end_ms,
                },
            )
            self._hold_usage_window(window, evidence)
            return None

    def _claim_usage_window(
        self,
        recorded_at: float,
    ) -> tuple[_UsageWindow, WorkerUsageEvidence] | None:
        """Claim before writing so the sampler and shutdown cannot bill overlapping windows.

        Retries retain their bounds and evidence because record IDs derive from
        those bounds. Widening a retry would charge the same usage under new IDs.
        """

        with self._lock:
            if self._held:
                return self._held.pop(0)
            start_ms = self._usage_cursor_ms
            elapsed_ms = int((recorded_at - self._started_at) * 1000)
            if elapsed_ms <= start_ms and start_ms > 0:
                return None
            # A container that existed for less than a millisecond still ran, and
            # a zero-length window is refused downstream as unbillable.
            end_ms = max(start_ms + 1, elapsed_ms)
            self._usage_cursor_ms = end_ms
            evidence = self._pending_usage_evidence
            self._pending_usage_evidence = WorkerUsageEvidence()
            complete = self._pending_measurement_complete and self._last_sample_at == recorded_at
            self._pending_measurement_complete = True
            return (
                _UsageWindow(start_ms=start_ms, end_ms=end_ms, measurement_complete=complete),
                evidence,
            )

    def _hold_usage_window(
        self,
        window: _UsageWindow,
        evidence: WorkerUsageEvidence,
    ) -> None:
        """Keep failed evidence separate from samples collected since its window.

        Both the sampler and shutdown can have a write in flight, so retain both
        failures. Claim no new window until these have been accepted.
        """

        with self._lock:
            self._held.append((window, evidence))


def _usage_evidence_from_metrics(metrics: ContainerMetricsData) -> WorkerUsageEvidence:
    interval_seconds = metrics.sample_interval_ms / 1_000
    return WorkerUsageEvidence(
        cpu_used_core_seconds=metrics.cpu_used / 1_000 * interval_seconds,
        memory_rss_byte_seconds=metrics.memory_rss_bytes * interval_seconds,
        memory_swap_byte_seconds=metrics.memory_swap_bytes * interval_seconds,
        disk_used_byte_seconds=metrics.disk_used_bytes * interval_seconds,
        gpu_memory_byte_seconds=metrics.gpu_memory_used_bytes * interval_seconds,
        network_ingress_bytes=metrics.network_recv_bytes,
        network_sent_bytes=metrics.network_sent_bytes,
        network_ingress_packets=metrics.network_recv_packets,
        network_egress_packets=metrics.network_sent_packets,
        disk_read_bytes=metrics.disk_read_bytes,
        disk_write_bytes=metrics.disk_write_bytes,
    )
