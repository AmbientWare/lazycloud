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
    def stop(self) -> ContainerRuntimeMonitoringResult: ...


class ContainerStateHeartbeatRepository(ContainerStatusUpdater, Protocol):
    """The scheduler's view of a container, read and re-armed by its worker.

    The write is `ContainerStatusUpdater`, which every finalization path already
    speaks; the read is what makes this a heartbeat rather than a report, since
    a state the platform has dropped must not be recreated.
    """

    def get_container_state(self, container_id: str) -> WorkerContainerState | None: ...


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
    """How many times the last drain of a container's life is attempted.

    The sample loop stops at the first refusal and lets the next tick carry the
    window, which is right while there is a next tick. At exit there is not one:
    whatever is still held when this returns is never offered again, so a single
    unlucky write would lose every second since the previous success.

    Bounded rather than persistent because this runs on the teardown path a
    customer is waiting on, and because a control plane that has refused three
    times in a row is not about to answer the fourth.
    """

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
        *,
        started_pid: int,
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
            started_pid=started_pid,
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
    started_pid: int
    metrics: WorkerContainerMetricsService | None
    usage_recorder: WorkerUsageWindowRecorder | None
    container_states: ContainerStateHeartbeatRepository | None
    settings: ContainerRuntimeMonitorSettings
    _started_at: float
    _started_at_utc: datetime
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
                # This thread is the only thing metering this container between
                # start and exit. Letting an exception out of it ends metering
                # silently for the rest of the container's life, and the daemon
                # thread dying is not something anything downstream observes —
                # the next signal would be a bill that is short.
                LOGGER.warning(
                    "container usage claim failed",
                    exc_info=True,
                    extra={"container_id": self.request.container_id},
                )

    def _heartbeat_container_state(self, *, recorded_at: float) -> None:
        """Re-arm the scheduler's record of this container while it is running.

        The state carries a TTL that is re-armed only by a write, and the worker
        writes `Running` exactly once at start. Without this tick the record
        simply expires under a container that is working perfectly well, and the
        orphan sweep then marks it failed — after which nothing counts it toward
        its stub's ceiling, the failure threshold starts counting it against the
        stub, and no stop is ever sent, so it keeps claiming. A container is
        allowed to outlive fifteen minutes; an invocation may take an hour.

        Paced against that TTL rather than against the sample loop it rides on.
        The two intervals answer different questions — how often this container
        is measured, and how long the platform waits before calling it gone — and
        tying the write to the first put a request pair on the control plane per
        container every few seconds to hold a window measured in minutes.

        A missing state is deliberately not rewritten. Recreating it would hide
        a container the platform has already decided it does not know about,
        which is the one case where letting the sweep reap it is correct.
        """

        if self.container_states is None or self._heartbeat_stopped:
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
                state_missing=state is None,
                runtime_started=True,
                runtime_pid=self.started_pid,
            )
            next_status = _heartbeat_next_status(plan)
            if next_status is None:
                # A state this thread is not the authority on: missing, or a stop
                # already in progress, or a record something has finished. Give
                # up the heartbeat rather than assert a liveness that is not this
                # thread's to assert — but only the heartbeat. Metrics and the
                # usage drain run off the same loop, and this container is still
                # consuming what they meter.
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
        """Emit every window this container owes the meter, oldest first.

        A window that failed is offered again before any new ground is claimed,
        so the platform sees the same bounds and the same evidence it saw the
        first time. Stopping at the first failure leaves the rest of the
        lifetime unclaimed rather than piling up windows against a control plane
        that is not answering.
        """

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
        """The next window owed: one held from a failure, or ground since the cursor.

        Claiming before the write rather than after is what keeps two emitters —
        the sample loop and a `stop()` whose join timed out on a slow write —
        from offering overlapping windows and billing the same seconds twice.

        A held window is re-offered with the bounds and the evidence it was
        claimed with, never widened to reach the present. The record ids the
        platform derives are a function of those bounds, so a write whose reply
        was lost after it committed is re-sent under the ids it already has and
        is refused as a duplicate. Widening instead would ask the platform to
        price ground it had already priced, under ids that cannot collide with
        the ones holding that charge.

        `None` once nothing is owed, which is how the drain loop ends.
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
        """Keep a failed window intact until the next attempt.

        The cursor stays past it, so samples taken since accumulate against the
        ground that follows rather than joining evidence measured over this
        window. That pairing is what stops a retry from charging current
        evidence against a window that did not measure it.

        A list because two emitters can be in flight at once — the sample loop
        and a `stop()` whose join timed out on a slow write — and a slot would
        let the second failure drop the first window's ground on the floor. It
        cannot grow past them: no new ground is claimed while anything is held.
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
