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

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

from shared.container_requests import (
    DEFAULT_MEMORY_PRESSURE_EVICTION_PERCENT,
    ContainerMemoryReading,
    StopContainerReason,
    select_memory_eviction_candidate,
)
from shared.contracts import ContractModel

MIB = 1024 * 1024


@dataclass(frozen=True, slots=True)
class ResidentContainer:
    """A container this worker is running, and what it was promised."""

    container_id: str
    pid: int
    reserved_mib: int


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
    read_memory_current: Callable[[int], int]
    stop_container: Callable[[str, StopContainerReason], None]
    threshold_percent: float = DEFAULT_MEMORY_PRESSURE_EVICTION_PERCENT
    evicted_container_ids: list[str] = field(default_factory=list, init=False)

    def run_once(self) -> MemoryEvictionResult:
        pressure = self.read_pressure_percent(self.worker_cgroup_path)
        if pressure < self.threshold_percent:
            return MemoryEvictionResult(pressure_percent=pressure, reason="machine is coping")

        readings = [
            ContainerMemoryReading(
                container_id=resident.container_id,
                current_bytes=self.read_memory_current(resident.pid),
                reserved_bytes=resident.reserved_mib * MIB,
            )
            for resident in self.residents()
            if resident.pid > 0
        ]
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

        self.stop_container(candidate.container_id, StopContainerReason.MemoryEvicted)
        self.evicted_container_ids.append(candidate.container_id)
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
