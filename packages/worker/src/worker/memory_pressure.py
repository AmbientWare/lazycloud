"""Evicting one container so the machine keeps serving the rest.

The kernel already does this, badly. `oom_badness` scores resident size and page
tables, so under pressure it reaches the largest container before the one that
outgrew what it reserved, and a tenant sitting inside its request is as eligible
as the tenant that tripled. That is the failure this exists to get ahead of: not
that a container dies, but that the wrong one does, chosen by a rule nobody
agreed to and reported as if the machine simply broke.

Getting ahead of it means acting while the kernel is still only reclaiming, which
is what the pressure reading is for.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

from shared.container_requests import (
    DEFAULT_MEMORY_PRESSURE_EVICTION_PERCENT,
    ContainerMemoryReading,
    StopContainerReason,
    select_memory_eviction_candidate,
)
from shared.contracts import ContractModel

LOGGER = logging.getLogger(__name__)

MIB = 1024 * 1024

# Long enough for an eviction to show up in the reading it was triggered by.
# `full avg10` is a ten-second average, so a machine that has just lost its
# largest over-committed container still reads as under pressure for most of
# that window.
MEMORY_PRESSURE_COOLDOWN_SECONDS = 15.0


@dataclass(frozen=True, slots=True)
class ResidentContainer:
    """A container this worker is running, and the cgroup that accounts for it."""

    container_id: str
    cgroup_path: str


class MemoryEvictionResult(ContractModel):
    """What one pass of the watcher decided, whether or not it acted."""

    pressure_percent: float = 0.0
    evicted_container_id: str = ""
    considered: int = 0
    reason: str = ""

    @property
    def evicted(self) -> bool:
        return bool(self.evicted_container_id)


@dataclass(slots=True)
class WorkerMemoryPressureWatcher:
    """Reads the machine every pass and stops at most one container.

    At most one, because reclaim takes time to show up in the pressure reading.
    Evicting everything over its reservation in a single pass would empty a
    machine that one departure would have settled.
    """

    worker_cgroup_path: str
    residents: Callable[[], Sequence[ResidentContainer]]
    read_pressure_percent: Callable[[str], float]
    read_memory_current: Callable[[str], int]
    read_memory_low: Callable[[str], int]
    stop_container: Callable[[str, StopContainerReason], None]
    monotonic: Callable[[], float] = time.monotonic
    threshold_percent: float = DEFAULT_MEMORY_PRESSURE_EVICTION_PERCENT
    cooldown_seconds: float = MEMORY_PRESSURE_COOLDOWN_SECONDS
    _evicted_at: float | None = field(default=None, init=False)

    def run_once(self) -> MemoryEvictionResult:
        pressure = self.read_pressure_percent(self.worker_cgroup_path)
        if pressure < self.threshold_percent:
            return MemoryEvictionResult(pressure_percent=pressure, reason="machine is coping")
        now = self.monotonic()
        if self._evicted_at is not None and now - self._evicted_at < self.cooldown_seconds:
            # The reading is an average over the last ten seconds, so it keeps
            # describing a shortage the previous eviction already settled. Acting
            # on it again empties a machine one departure would have fixed.
            return MemoryEvictionResult(
                pressure_percent=pressure,
                reason="waiting to see whether the last eviction settled it",
            )

        # Both numbers come from the container's own cgroup, which is the only
        # place they mean the same thing. The sandbox process's RSS is a different
        # accounting from what the cgroup is charged -- under gVisor the sentry
        # and the gofer are charged here alongside guest memory -- so measuring
        # one against a reservation expressed in the other made every container
        # look larger than its promise by its own sandbox, and the smallest ones
        # look furthest over. `memory.current` against `memory.low` is the pair
        # the kernel itself weighs when it decides what to reclaim, and reading
        # the protection rather than remembering it means this inherits whatever
        # the planner wrote, including any sandbox allowance added later.
        readings: list[ContainerMemoryReading] = []
        for resident in self.residents():
            if not resident.cgroup_path:
                continue
            reserved = self.read_memory_low(resident.cgroup_path)
            # A container protected for nothing is not one that reserved zero:
            # its whole footprint would count as excess and it would be chosen
            # every pass, told it was furthest above a request it never made.
            # `memory.low` reads `0` both when unset and when the cgroup is gone.
            if reserved <= 0:
                continue
            readings.append(
                ContainerMemoryReading(
                    container_id=resident.container_id,
                    current_bytes=self.read_memory_current(resident.cgroup_path),
                    reserved_bytes=reserved,
                )
            )
        candidate = select_memory_eviction_candidate(
            readings,
            pressure_percent=pressure,
            threshold_percent=self.threshold_percent,
        )
        if candidate is None:
            # Every container is inside what it reserved, so none of them caused
            # this and stopping one would be arbitrary. The machine is
            # oversubscribed by something this watcher does not place.
            return MemoryEvictionResult(
                pressure_percent=pressure,
                considered=len(readings),
                reason="nothing is above its reservation",
            )

        try:
            self.stop_container(candidate.container_id, StopContainerReason.MemoryEvicted)
        except Exception as error:
            # Reported, not raised. This runs inside the worker's main loop, and
            # letting it escape skips the rest of the pass -- the loop stops
            # taking scheduler work while the machine is still short of memory.
            LOGGER.warning(
                "could not evict %s: %s: %s",
                candidate.container_id,
                type(error).__name__,
                error,
            )
            return MemoryEvictionResult(
                pressure_percent=pressure,
                considered=len(readings),
                reason=f"eviction of {candidate.container_id} failed",
            )
        # Only once something was actually stopped. A cooldown started by a
        # no-op leaves the machine in stall for its whole length with the
        # watcher declining to act.
        self._evicted_at = now
        return MemoryEvictionResult(
            pressure_percent=pressure,
            evicted_container_id=candidate.container_id,
            considered=len(readings),
            reason=(
                f"{candidate.bytes_above_reservation // MIB} MiB above its reservation, "
                f"the most on this machine"
            ),
        )


__all__ = [
    "MemoryEvictionResult",
    "ResidentContainer",
    "WorkerMemoryPressureWatcher",
]
