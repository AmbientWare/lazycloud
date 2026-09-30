from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass
from time import monotonic

from coordination.redis_client import REDIS_UNAVAILABLE_ERRORS
from scheduler.reconciliation import SchedulerRunResult

from scheduler_app.fleet_coordinator import FleetCoordinator
from scheduler_app.loops import SchedulerLoop, SchedulerLoopSupervisor, run_loop

LOGGER = logging.getLogger(__name__)
ACQUISITION_CONTENDED_RETRY_SECONDS = 1.0


@dataclass(slots=True)
class _ContendedDemand:
    """Demand the acquisition loop retries alone after it met a held lease."""

    container_ids: tuple[str, ...] = ()
    attempts: int = 0
    full_sweep: bool = True

    @property
    def retrying(self) -> tuple[str, ...]:
        return () if self.full_sweep else self.container_ids

    def observe(self, contended: tuple[str, ...]) -> None:
        self.attempts = self.attempts + 1 if contended and not self.full_sweep else 0
        self.container_ids = contended
        self.full_sweep = not contended

    def delay_seconds(self) -> float:
        return ACQUISITION_CONTENDED_RETRY_SECONDS * (1 << min(self.attempts, 3))

    def sweep_next(self) -> None:
        self.full_sweep = True


def start_fleet_loops(
    controller: FleetCoordinator,
    *,
    stop: threading.Event,
    beats: dict[str, Callable[[], None]],
    container_limit: int = 100,
) -> SchedulerLoopSupervisor:
    loops: list[SchedulerLoop] = []

    def wait_for_wake(timeout: float) -> bool:
        wake = controller.wake
        try:
            deadline = monotonic() + timeout
            while not stop.is_set():
                remaining = deadline - monotonic()
                if remaining <= 0:
                    return False
                if wake.wait(timeout_seconds=min(remaining, 1.0)):
                    return True
        except REDIS_UNAVAILABLE_ERRORS:
            LOGGER.warning("fleet wake unavailable; durable demand remains due")
            stop.wait(timeout)
        return False

    def spawn(
        name: str,
        interval: float,
        action: Callable[[], SchedulerRunResult],
        wait: Callable[[float], None] | None = None,
    ) -> None:
        beat = beats.get(name)
        thread = threading.Thread(
            target=run_loop,
            kwargs=dict(
                name=name,
                stop=stop,
                interval_seconds=interval,
                pass_once=action,
                beat=beat,
                wait=wait,
            ),
            name=f"fleet-{name}",
            daemon=True,
        )
        thread.start()
        loops.append(SchedulerLoop(name, thread, beat))

    retry = _ContendedDemand()

    def acquire() -> SchedulerRunResult:
        result = controller.run_acquisition_pass(
            container_limit=container_limit, retry_container_ids=retry.retrying
        )
        retry.observe(tuple(result.capacity_demand_contended))
        return result

    def wait_for_acquisition(timeout: float) -> None:
        if not retry.container_ids:
            wait_for_wake(timeout)
        elif wait_for_wake(min(retry.delay_seconds(), timeout)):
            retry.sweep_next()

    spawn("acquisition", 5.0, acquire, wait_for_acquisition)
    spawn("capacity", 5.0, lambda: controller.run_capacity_pass(container_limit=container_limit))
    spawn(
        "housekeeping",
        30.0,
        lambda: controller.run_housekeeping_pass(container_limit=container_limit),
    )
    return SchedulerLoopSupervisor(stop=stop, loops=loops)
