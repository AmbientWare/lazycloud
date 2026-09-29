from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from threading import Lock
from time import monotonic
from typing import ParamSpec, TypeVar

P = ParamSpec("P")
T = TypeVar("T")


@dataclass(frozen=True, slots=True)
class ExecutorPressure:
    capacity: int
    active: int
    pending: int
    started: int
    queue_seconds_total: float
    queue_seconds_max: float


class MeasuredThreadPoolExecutor(ThreadPoolExecutor):
    def __init__(self, *, max_workers: int, thread_name_prefix: str) -> None:
        super().__init__(max_workers=max_workers, thread_name_prefix=thread_name_prefix)
        self.capacity = max_workers
        self._pressure_lock = Lock()
        self._active = 0
        self._pending = 0
        self._started = 0
        self._queue_seconds_total = 0.0
        self._queue_seconds_max = 0.0

    def submit(self, fn: Callable[P, T], /, *args: P.args, **kwargs: P.kwargs) -> Future[T]:
        queued_at = monotonic()
        with self._pressure_lock:
            self._pending += 1

        def run() -> T:
            queue_seconds = monotonic() - queued_at
            with self._pressure_lock:
                self._pending -= 1
                self._active += 1
                self._started += 1
                self._queue_seconds_total += queue_seconds
                self._queue_seconds_max = max(self._queue_seconds_max, queue_seconds)
            try:
                return fn(*args, **kwargs)
            finally:
                with self._pressure_lock:
                    self._active -= 1

        try:
            future = super().submit(run)
        except BaseException:
            with self._pressure_lock:
                self._pending -= 1
            raise

        def finished(result: Future[T]) -> None:
            if result.cancelled():
                with self._pressure_lock:
                    self._pending -= 1

        future.add_done_callback(finished)
        return future

    def pressure(self) -> ExecutorPressure:
        with self._pressure_lock:
            return ExecutorPressure(
                capacity=self.capacity,
                active=self._active,
                pending=self._pending,
                started=self._started,
                queue_seconds_total=self._queue_seconds_total,
                queue_seconds_max=self._queue_seconds_max,
            )
