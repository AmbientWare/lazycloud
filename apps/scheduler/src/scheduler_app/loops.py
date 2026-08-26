"""The scheduler's loops, and the supervisor that owns their lifetime.

One process, several cadences. Placement answers in milliseconds because a
caller is waiting for it; capacity keeps the fleet and its records agreeing;
housekeeping waits on Stripe, S3, Tailscale and Cloudflare, which answer on
their own schedule. Running all of it on one thread meant placement waited for
the slowest of them, and a task took fifty-five seconds to start behind a
Stripe drain.

Each loop is separate because its cadence is, not because its work is
unrelated. What makes that safe is that every reconciliation already guards
itself: the interval stamps on `Scheduler` are each written and read by one
reconciliation, and the ones with non-idempotent effects take their own Redis
lease. Keeping a reconciliation in exactly one loop is the whole of the
discipline here.
"""

from __future__ import annotations

import logging
import signal
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from types import FrameType

from scheduler.service import Scheduler, SchedulerRunResult

LOGGER = logging.getLogger(__name__)

PLACEMENT_SWEEP_INTERVAL_SECONDS = 0.25
"""How often placement looks for work that has nowhere to run.

A plain timed sweep rather than a wake, deliberately. `RedisWakeSignal.wait` is a
blocking pop, so it consumes what it receives: a second waiter on the dispatch
scope would take wakeups meant for dispatch and delay the loop it was trying to
help. Placement would need a scope of its own, published from wherever a stub's
backlog grows, and at this interval that buys under a quarter second against a
sweep that costs one indexed query. It is worth doing when the sweep is what
limits latency, and it is not.
"""

CAPACITY_INTERVAL_SECONDS = 5.0
"""Cadence for the pass that decides what capacity exists.

Also the cadence billing enforcement runs on, and that is the binding
constraint: every second between an account running out and its containers
stopping is spend that will not be collected.
"""

HOUSEKEEPING_INTERVAL_SECONDS = 30.0
"""Cadence for everything that waits on somebody else's service.

Coarse on purpose. Nothing placement needs is produced here, and the meter
outbox is durable, so a slower drain delays delivery rather than losing it.
"""

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
        for loop in self.loops:
            loop.thread.join(timeout=timeout_seconds)
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
    """Run one pass forever, on its own failure budget.

    The backoff counter is per loop rather than per process. Sharing one meant a
    housekeeping pass that could not reach Stripe throttled placement, which is
    the coupling this whole arrangement exists to remove.

    The beat is stamped after the pass, not before. Stamped before, it says the
    loop entered a pass; what an operator needs to know is that one finished.
    """

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
    include_cron_jobs: bool = True,
    include_containers: bool = True,
    container_limit: int = 100,
    capacity_interval_seconds: float = CAPACITY_INTERVAL_SECONDS,
    housekeeping_interval_seconds: float = HOUSEKEEPING_INTERVAL_SECONDS,
    beats: dict[str, Callable[[], None]] | None = None,
    stop: threading.Event | None = None,
) -> SchedulerLoopSupervisor:
    """Start every loop this process runs and hand back the means to stop them."""

    resolved_stop = stop or threading.Event()
    resolved_beats = beats or {}
    loops: list[SchedulerLoop] = []

    def spawn(
        name: str,
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

    spawn(
        "placement",
        PLACEMENT_SWEEP_INTERVAL_SECONDS,
        lambda: scheduler.run_placement_pass(
            include_containers=include_containers,
            container_limit=container_limit,
        ),
    )
    spawn(
        "capacity",
        capacity_interval_seconds,
        lambda: scheduler.run_capacity_pass(
            include_cron_jobs=include_cron_jobs,
            include_containers=include_containers,
            container_limit=container_limit,
        ),
    )
    spawn(
        "housekeeping",
        housekeeping_interval_seconds,
        lambda: scheduler.run_housekeeping_pass(
            include_containers=include_containers,
            container_limit=container_limit,
        ),
    )
    if include_containers:
        dispatch = threading.Thread(
            target=scheduler.run_container_dispatch_loop,
            kwargs={
                "stop": resolved_stop,
                "container_limit": container_limit,
                "beat": resolved_beats.get("dispatch"),
            },
            name="scheduler-container-dispatch",
            daemon=True,
        )
        dispatch.start()
        loops.append(
            SchedulerLoop(
                name="dispatch",
                thread=dispatch,
                beat=resolved_beats.get("dispatch"),
            )
        )
    return SchedulerLoopSupervisor(stop=resolved_stop, loops=loops)


@contextmanager
def scheduler_shutdown_handlers(
    stop: threading.Event,
    *,
    enabled: bool = True,
) -> Iterator[None]:
    """Turn the signal Kubernetes sends into an ordered stop.

    Without this the process dies where it stands on SIGTERM: no telemetry
    flush, no thread join, no lease released. Handlers install only on the main
    thread, because that is the only thread Python allows them on.
    """

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
    "CAPACITY_INTERVAL_SECONDS",
    "HOUSEKEEPING_INTERVAL_SECONDS",
    "PLACEMENT_SWEEP_INTERVAL_SECONDS",
    "SchedulerLoop",
    "SchedulerLoopSupervisor",
    "run_loop",
    "scheduler_shutdown_handlers",
    "start_scheduler_loops",
]


SCHEDULER_LOOP_NAMES: tuple[str, ...] = ("placement", "capacity", "housekeeping", "dispatch")
"""Every loop that stamps a heartbeat, in the order an operator reads them."""

LOOP_SUPERVISOR_POLL_SECONDS = 1.0
"""How often the main thread checks whether a signal asked it to stop."""

LOOP_SHUTDOWN_TIMEOUT_SECONDS = 10.0
"""How long a loop is given to finish the pass it is in before it is abandoned.

Longer than any pass should take and shorter than the grace period Kubernetes
allows, so an ordinary stop is ordered and a wedged one still terminates.
"""
