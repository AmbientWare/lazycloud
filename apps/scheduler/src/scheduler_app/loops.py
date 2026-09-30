from __future__ import annotations

import logging
import signal
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from time import monotonic
from types import FrameType

from coordination.redis_client import REDIS_UNAVAILABLE_ERRORS
from coordination.wake_signal import WakeSignalWaiter
from scheduler.reconciliation import DEFAULT_AUTOSCALING_RECONCILE_LIMIT, SchedulerRunResult
from scheduler.service import Scheduler

from scheduler_app.health import SchedulerLoopName

LOGGER = logging.getLogger(__name__)

PLACEMENT_SWEEP_INTERVAL_SECONDS = 1.0
"""Wake wait bound used to observe shutdown and heartbeat progress."""

PLACEMENT_MIN_INTERVAL_SECONDS = 0.1
"""Coalesce demand bursts before another placement snapshot."""

LOOP_FAILURE_RETRY_MAX_SECONDS = 30.0


@dataclass(frozen=True, slots=True)
class SchedulerLoop:
    """One running loop and the switch that stops it."""

    name: str
    thread: threading.Thread
    beat: Callable[[], None] | None = None


@dataclass(slots=True)
class SchedulerLoopSupervisor:
    """Every loop the process runs, stopped together.

    One event for all of them, so a signal handler has one thing to set and
    teardown cannot stop half a scheduler.
    """

    stop: threading.Event
    loops: list[SchedulerLoop]

    def shutdown(self, *, timeout_seconds: float) -> None:
        self.stop.set()
        deadline = monotonic() + timeout_seconds
        for loop in self.loops:
            loop.thread.join(timeout=max(0.0, deadline - monotonic()))
            if loop.thread.is_alive():
                LOGGER.warning("scheduler loop %s did not stop promptly", loop.name)


def run_loop(
    *,
    name: str,
    stop: threading.Event,
    interval_seconds: float,
    pass_once: Callable[[], SchedulerRunResult],
    beat: Callable[[], None] | None = None,
    wait: Callable[[float], None] | None = None,
) -> None:
    """Back off failures per loop and stamp progress only after completed work."""

    consecutive_failures = 0
    while not stop.is_set():
        try:
            pass_once()
        except Exception:
            consecutive_failures += 1
            retry_seconds = min(
                LOOP_FAILURE_RETRY_MAX_SECONDS,
                max(interval_seconds, 0.1) * (1 << min(consecutive_failures - 1, 8)),
            )
            LOGGER.exception(
                "scheduler loop %s failed; retrying",
                name,
                extra={
                    "loop": name,
                    "consecutive_failures": consecutive_failures,
                    "retry_seconds": retry_seconds,
                },
            )
            stop.wait(retry_seconds)
            continue
        consecutive_failures = 0
        if beat is not None:
            beat()
        if wait is not None:
            wait(interval_seconds)
        else:
            stop.wait(interval_seconds)


def start_scheduler_loops(
    scheduler: Scheduler,
    *,
    dispatch_wake: WakeSignalWaiter,
    placement_wake: WakeSignalWaiter,
    include_cron_jobs: bool = True,
    include_containers: bool = True,
    container_limit: int = 100,
    autoscaling_limit: int = DEFAULT_AUTOSCALING_RECONCILE_LIMIT,
    beats: dict[str, Callable[[], None]] | None = None,
    stop: threading.Event | None = None,
) -> SchedulerLoopSupervisor:
    """Start every loop this process runs and hand back the means to stop them."""

    resolved_stop = stop or threading.Event()
    resolved_beats = beats or {}
    loops: list[SchedulerLoop] = []

    def wait_for_wake(wake: WakeSignalWaiter, timeout: float) -> bool:
        try:
            deadline = monotonic() + timeout
            while not resolved_stop.is_set():
                remaining = deadline - monotonic()
                if remaining <= 0:
                    return False
                # Stay below Redis's socket timeout and observe process shutdown between reads.
                if wake.wait(timeout_seconds=min(remaining, 1.0)):
                    return True
        except REDIS_UNAVAILABLE_ERRORS:
            LOGGER.warning("scheduler wake unavailable; using the durable due-work sweep")
            resolved_stop.wait(timeout)
        return False

    def spawn(
        name: SchedulerLoopName,
        interval_seconds: float,
        pass_once: Callable[[], SchedulerRunResult],
        wait: Callable[[float], None] | None = None,
    ) -> None:
        beat = resolved_beats.get(name)
        thread = threading.Thread(
            target=run_loop,
            kwargs={
                "name": name,
                "stop": resolved_stop,
                "interval_seconds": interval_seconds,
                "pass_once": pass_once,
                "beat": beat,
                "wait": wait,
            },
            name=f"scheduler-{name}",
            daemon=True,
        )
        thread.start()
        loops.append(SchedulerLoop(name=name, thread=thread, beat=beat))

    last_placement_started = 0.0
    demand_pending = False

    def place() -> SchedulerRunResult:
        nonlocal last_placement_started, demand_pending
        if not demand_pending:
            return SchedulerRunResult()
        demand_pending = False
        last_placement_started = monotonic()
        return scheduler.run_placement_pass(
            include_containers=include_containers,
            autoscaling_limit=autoscaling_limit,
        )

    def wait_for_placement(timeout: float) -> None:
        nonlocal demand_pending
        demand_pending = wait_for_wake(placement_wake, timeout)
        remaining = PLACEMENT_MIN_INTERVAL_SECONDS - (monotonic() - last_placement_started)
        if remaining > 0:
            resolved_stop.wait(remaining)

    spawn(
        SchedulerLoopName.Placement,
        PLACEMENT_SWEEP_INTERVAL_SECONDS,
        place,
        wait=wait_for_placement,
    )
    spawn(
        SchedulerLoopName.Recovery,
        0.1,
        lambda: scheduler.run_recovery_pass(
            include_containers=include_containers,
            container_limit=container_limit,
            autoscaling_limit=autoscaling_limit,
        ),
    )
    spawn(
        SchedulerLoopName.Scheduled,
        1.0,
        lambda: (
            scheduler.run_scheduled_pass(limit=container_limit)
            if include_cron_jobs
            else SchedulerRunResult()
        ),
    )
    spawn(
        SchedulerLoopName.Builds,
        1.0,
        lambda: (
            scheduler.run_build_pass(container_limit=container_limit)
            if include_containers
            else SchedulerRunResult()
        ),
    )
    if include_containers:

        def dispatch() -> SchedulerRunResult:
            batch_limit = max(container_limit, 1)
            while not resolved_stop.is_set():
                batch = scheduler.containers.dispatch_ready(limit=batch_limit)
                for result in batch:
                    LOGGER.info(
                        "container dispatch %s: status=%s reason=%s",
                        result.container_id,
                        result.status.value,
                        result.reason,
                        extra={"container_id": result.container_id, "worker_id": result.worker_id},
                    )
                if len(batch) < batch_limit:
                    break
            return SchedulerRunResult()

        def wait_for_dispatch(timeout: float) -> None:
            wait_for_wake(dispatch_wake, timeout)

        spawn(
            SchedulerLoopName.Dispatch,
            1.0,
            dispatch,
            wait=wait_for_dispatch,
        )
    return SchedulerLoopSupervisor(stop=resolved_stop, loops=loops)


@contextmanager
def scheduler_shutdown_handlers(
    stop: threading.Event,
    *,
    enabled: bool = True,
) -> Iterator[None]:
    """Signal handlers install on the main thread and request an ordered stop."""

    if not enabled or threading.current_thread() is not threading.main_thread():
        yield
        return
    signals = (signal.SIGTERM, signal.SIGINT)

    def request_shutdown(signum: int, _frame: FrameType | None) -> None:
        LOGGER.info("scheduler received signal %s; stopping loops", signum)
        stop.set()

    previous = {signum: signal.signal(signum, request_shutdown) for signum in signals}
    try:
        yield
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)


__all__ = [
    "PLACEMENT_SWEEP_INTERVAL_SECONDS",
    "SchedulerLoop",
    "SchedulerLoopSupervisor",
    "run_loop",
    "scheduler_shutdown_handlers",
    "start_scheduler_loops",
]


LOOP_SUPERVISOR_POLL_SECONDS = 1.0
"""How often the main thread checks whether a signal asked it to stop."""

LOOP_SHUTDOWN_TIMEOUT_SECONDS = 10.0
"""How long a loop is given to finish the pass it is in before it is abandoned.

Longer than any pass should take and shorter than the grace period Kubernetes
allows, so an ordinary stop is ordered and a wedged one still terminates.
"""
