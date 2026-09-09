from __future__ import annotations

import logging
import threading
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from time import monotonic

from shared.timestamps import utc_now

from worker.events import ContainerRequestContext, WorkerUsageEvidence
from worker.image_build_resources import ImageBuildResources
from worker.monitoring import WorkerUsageWindowRecorder

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class _Window:
    start_ms: int
    end_ms: int
    evidence: WorkerUsageEvidence


@dataclass(slots=True)
class ImageBuildMetering:
    resources: ImageBuildResources
    request: ContainerRequestContext
    recorder: WorkerUsageWindowRecorder
    started_at: datetime = field(default_factory=utc_now)
    _start: float = field(default_factory=monotonic)
    _stop: threading.Event = field(default_factory=threading.Event)
    _finished: threading.Event = field(default_factory=threading.Event)
    _ready: threading.Event = field(default_factory=threading.Event)
    _lock: threading.Lock = field(default_factory=threading.Lock)
    _windows: deque[_Window] = field(default_factory=deque)
    _sampler: threading.Thread | None = None
    _publisher: threading.Thread | None = None
    _previous_ms: int = 0
    _window_start_ms: int = 0
    _previous_cpu: int = 0
    _previous_memory: int = 0
    _cpu_seconds: float = 0
    _memory_byte_seconds: float = 0
    _ended_ms: int = 0
    _measurement_failed: bool = False
    _delivery_deadline: float | None = None

    def start(self) -> None:
        self._previous_cpu, self._previous_memory = self.resources.counters()
        self._sampler = threading.Thread(
            target=self._sample_loop, daemon=True, name=f"build-meter-{self.request.container_id}"
        )
        self._publisher = threading.Thread(
            target=self._publish_loop, daemon=True, name=f"build-usage-{self.request.container_id}"
        )
        self._sampler.start()
        self._publisher.start()

    def close(self) -> datetime:
        self._stop.set()
        try:
            if self._sampler is not None:
                self._sampler.join()
            if not self._measurement_failed and not self._finished.is_set():
                self._sample(final=True)
        finally:
            self._delivery_deadline = monotonic() + 10
            self._finished.set()
            self._ready.set()
            if self._publisher is not None:
                self._publisher.join(timeout=10)
                if self._publisher.is_alive():
                    self._report_pending_usage("image build usage delivery remains in flight")
        if self._measurement_failed:
            raise RuntimeError("image build resource measurement failed")
        return self.started_at + timedelta(milliseconds=self._ended_ms)

    def _sample_loop(self) -> None:
        while not self._stop.wait(1):
            try:
                final = self.resources.stopped_monotonic is not None
                self._sample(final=final)
                if final:
                    self._finished.set()
                    self._ready.set()
                    return
            except Exception:
                self._measurement_failed = True
                LOGGER.exception(
                    "image build measurement failed",
                    extra={"container_id": self.request.container_id},
                )
                self.resources.stop()
                return

    def _sample(self, *, final: bool) -> None:
        end = self.resources.stopped_monotonic or monotonic()
        end_ms = max(int((end - self._start) * 1000), 1)
        cpu, memory = self.resources.counters()
        if cpu < self._previous_cpu:
            raise RuntimeError("image build CPU counter regressed")
        self._cpu_seconds += (cpu - self._previous_cpu) / 1_000_000
        self._memory_byte_seconds += self._previous_memory * (end_ms - self._previous_ms) / 1000
        self._previous_cpu, self._previous_memory = cpu, memory
        self._previous_ms = end_ms
        self._ended_ms = end_ms
        if end_ms > self._window_start_ms and (final or end_ms - self._window_start_ms >= 5000):
            window = _Window(
                self._window_start_ms,
                end_ms,
                WorkerUsageEvidence(
                    cpu_used_core_seconds=self._cpu_seconds,
                    memory_rss_byte_seconds=self._memory_byte_seconds,
                ),
            )
            with self._lock:
                self._windows.append(window)
            self._window_start_ms = end_ms
            self._cpu_seconds = 0
            self._memory_byte_seconds = 0
            self._ready.set()

    def _publish_loop(self) -> None:
        final_failures = 0
        while True:
            self._ready.wait(1)
            self._ready.clear()
            with self._lock:
                window = self._windows[0] if self._windows else None
            if window is None:
                if self._finished.is_set():
                    return
                continue
            if self._delivery_deadline is not None and monotonic() >= self._delivery_deadline:
                self._report_pending_usage("image build final usage delivery deadline reached")
                return
            try:
                result = self.recorder.record_usage_window(
                    self.request,
                    duration_ms=window.end_ms - window.start_ms,
                    window_start_ms=window.start_ms,
                    window_end_ms=window.end_ms,
                    metering_window_started_at=self.started_at
                    + timedelta(milliseconds=window.start_ms),
                    metering_window_ended_at=self.started_at
                    + timedelta(milliseconds=window.end_ms),
                    evidence=window.evidence,
                    measurement_complete=True,
                )
                if result.skipped:
                    raise RuntimeError("image build usage window was not recorded")
            except Exception:
                LOGGER.warning(
                    "image build usage delivery failed",
                    extra={"container_id": self.request.container_id},
                    exc_info=True,
                )
                if self._finished.is_set():
                    final_failures += 1
                    if final_failures >= 3:
                        self._report_pending_usage("image build final usage retries exhausted")
                        return
                continue
            with self._lock:
                self._windows.popleft()
                if self._windows:
                    self._ready.set()

    def _report_pending_usage(self, message: str) -> None:
        with self._lock:
            windows = [(window.start_ms, window.end_ms) for window in self._windows]
        LOGGER.warning(
            message,
            extra={"container_id": self.request.container_id, "pending_windows_ms": windows},
        )
