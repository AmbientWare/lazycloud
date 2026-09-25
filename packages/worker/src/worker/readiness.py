from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from concurrent.futures import Future
from dataclasses import dataclass, field
from threading import Lock, Thread

from shared.step_timings import StepTimings

LOGGER = logging.getLogger(__name__)
READINESS_SHUTDOWN_SECONDS = 10.0


def run_readiness_checks[T](label: str, checks: Mapping[str, Callable[[], T]]) -> dict[str, T]:
    """Run independent checks together without keeping a shutting-down worker alive."""
    timings = StepTimings()
    outcomes: dict[str, Future[T]] = {}

    def run(name: str, check: Callable[[], T], outcome: Future[T]) -> None:
        try:
            with timings.step(name):
                result = check()
        except BaseException as exc:
            outcome.set_exception(exc)
        else:
            outcome.set_result(result)

    for name, check in checks.items():
        outcome: Future[T] = Future()
        outcomes[name] = outcome
        Thread(
            target=run, args=(name, check, outcome), name=f"readiness-{name}", daemon=True
        ).start()
    results: dict[str, T] = {}
    failure: BaseException | None = None
    try:
        for name, outcome in outcomes.items():
            error = outcome.exception()
            if error is not None:
                if failure is None:
                    failure = error
                else:
                    LOGGER.error("readiness check %s failed", name, exc_info=error)
            else:
                results[name] = outcome.result()
        if failure is not None:
            raise failure
        return results
    finally:
        timings.log(LOGGER, label)


@dataclass(slots=True)
class WorkerReadiness:
    preparation_checks: Mapping[str, Callable[[], None]] = field(
        default_factory=dict[str, Callable[[], None]]
    )
    validation_checks: Mapping[str, Callable[[], None]] = field(
        default_factory=dict[str, Callable[[], None]]
    )
    preparation: Future[None] = field(default_factory=Future, init=False)
    _lock: Lock = field(default_factory=Lock, init=False)
    _started: bool = field(default=False, init=False)
    _closed: bool = field(default=False, init=False)

    def start(self) -> None:
        with self._lock:
            if self._closed:
                raise RuntimeError("worker readiness is closed")
            if self._started:
                return
            self._started = True
            Thread(target=self._prepare, name="worker-readiness", daemon=True).start()

    def _prepare(self) -> None:
        try:
            run_readiness_checks("worker readiness preparation", self.preparation_checks)
        except BaseException as exc:
            self.preparation.set_exception(exc)
        else:
            self.preparation.set_result(None)

    def validate(self) -> None:
        self.start()
        self.preparation.result()
        run_readiness_checks("worker readiness validation", self.validation_checks)

    def close(self, *, timeout_seconds: float = READINESS_SHUTDOWN_SECONDS) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            started = self._started
        if not started:
            return
        try:
            error = self.preparation.exception(timeout=timeout_seconds)
        except TimeoutError:
            LOGGER.warning("worker readiness preparation exceeded its shutdown deadline")
            return
        if error is not None:
            LOGGER.warning("worker readiness preparation failed", exc_info=error)
