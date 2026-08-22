from __future__ import annotations

from shared.container_requests import StopContainerReason
from worker.memory_pressure import ResidentContainer, WorkerMemoryPressureWatcher

MIB = 1024 * 1024


def _watcher(
    *,
    pressure: float,
    residents: list[ResidentContainer],
    charged_mib: dict[str, int],
    protected_mib: dict[str, int],
    stopped: list[tuple[str, StopContainerReason]],
    clock: list[float] | None = None,
) -> WorkerMemoryPressureWatcher:
    ticks = clock if clock is not None else [0.0]
    return WorkerMemoryPressureWatcher(
        worker_cgroup_path="/sys/fs/cgroup/worker",
        residents=lambda: residents,
        read_pressure_percent=lambda _path: pressure,
        read_memory_current=lambda path: charged_mib.get(path, 0) * MIB,
        read_memory_low=lambda path: protected_mib.get(path, 0) * MIB,
        stop_container=lambda container_id, reason: stopped.append((container_id, reason)),
        monotonic=lambda: ticks[0],
    )


def test_a_container_protected_for_nothing_is_not_the_victim() -> None:
    """Protected for nothing is not the same as reserving zero.

    Its whole footprint would count as excess, so it would outrank every genuinely
    over-committed tenant and be stopped every pass, told it was furthest above a
    request it never made. `memory.low` reads zero both when it was never written
    and when the cgroup has gone.
    """
    stopped: list[tuple[str, StopContainerReason]] = []
    result = _watcher(
        pressure=90.0,
        residents=[
            ResidentContainer("unprotected", "/cg/unprotected"),
            ResidentContainer("over", "/cg/over"),
        ],
        charged_mib={"/cg/unprotected": 16384, "/cg/over": 2048},
        protected_mib={"/cg/unprotected": 0, "/cg/over": 1024},
        stopped=stopped,
    ).run_once()

    assert result.evicted_container_id == "over"
    assert stopped == [("over", StopContainerReason.MemoryEvicted)]


def test_the_largest_container_is_spared_for_the_one_that_outgrew_its_promise() -> None:
    """The whole reason the choice is not left to the kernel.

    `oom_badness` scores resident size, so it reaches an eight-gibibyte tenant
    sitting inside what it reserved before a one-gibibyte tenant that tripled.
    """
    stopped: list[tuple[str, StopContainerReason]] = []
    result = _watcher(
        pressure=90.0,
        residents=[
            ResidentContainer("honest", "/cg/honest"),
            ResidentContainer("leaker", "/cg/leaker"),
        ],
        charged_mib={"/cg/honest": 8192, "/cg/leaker": 3072},
        protected_mib={"/cg/honest": 8192, "/cg/leaker": 1024},
        stopped=stopped,
    ).run_once()

    assert result.evicted_container_id == "leaker"


def test_a_container_without_a_cgroup_cannot_be_weighed() -> None:
    """No cgroup means no accounting, and a guess here stops the wrong tenant."""
    stopped: list[tuple[str, StopContainerReason]] = []
    result = _watcher(
        pressure=90.0,
        residents=[ResidentContainer("unplaced", "")],
        charged_mib={},
        protected_mib={},
        stopped=stopped,
    ).run_once()

    assert not result.evicted
    assert stopped == []


def test_the_next_pass_waits_to_see_whether_the_first_eviction_settled_it() -> None:
    """The reading averages ten seconds, so it outlives what caused it.

    Without a wait, a shortage one departure had already fixed keeps reading as
    pressure and the machine is emptied a container at a time, which is the
    outcome the one-per-pass rule only ever prevented inside a single pass.
    """
    stopped: list[tuple[str, StopContainerReason]] = []
    clock = [0.0]
    watcher = _watcher(
        pressure=90.0,
        residents=[
            ResidentContainer("a", "/cg/a"),
            ResidentContainer("b", "/cg/b"),
        ],
        charged_mib={"/cg/a": 8192, "/cg/b": 4096},
        protected_mib={"/cg/a": 1024, "/cg/b": 1024},
        stopped=stopped,
        clock=clock,
    )

    assert watcher.run_once().evicted
    clock[0] = 2.0
    assert not watcher.run_once().evicted
    clock[0] = 30.0
    assert watcher.run_once().evicted
    assert len(stopped) == 2
