from __future__ import annotations

from shared.container_requests import StopContainerReason
from worker.memory_pressure import ResidentContainer, WorkerMemoryPressureWatcher

MIB = 1024 * 1024


def _watcher(
    *,
    pressure: float,
    residents: list[ResidentContainer],
    current_mib: dict[int, int],
    stopped: list[tuple[str, StopContainerReason]],
    clock: list[float] | None = None,
) -> WorkerMemoryPressureWatcher:
    ticks = clock if clock is not None else [0.0]
    return WorkerMemoryPressureWatcher(
        worker_cgroup_path="/sys/fs/cgroup/worker",
        residents=lambda: residents,
        read_pressure_percent=lambda _path: pressure,
        read_memory_current=lambda pid: current_mib[pid] * MIB,
        stop_container=lambda container_id, reason: stopped.append((container_id, reason)),
        monotonic=lambda: ticks[0],
    )


def test_a_container_is_invisible_until_it_reports_a_sandbox() -> None:
    """A pid arrives after registration, and nothing works until it does.

    A long-running container has no sandbox when it is registered and reports
    none again until it exits. Left at zero it is filtered out of every pass, so
    the machine fills with containers the watcher cannot see and the kernel makes
    the decision after all.
    """
    stopped: list[tuple[str, StopContainerReason]] = []
    without_pid = _watcher(
        pressure=90.0,
        residents=[ResidentContainer("leaker", 0, reserved_mib=1024)],
        current_mib={102: 8192},
        stopped=stopped,
    )
    assert not without_pid.run_once().evicted
    assert stopped == []

    with_pid = _watcher(
        pressure=90.0,
        residents=[ResidentContainer("leaker", 102, reserved_mib=1024)],
        current_mib={102: 8192},
        stopped=stopped,
    )
    assert with_pid.run_once().evicted


def test_a_container_that_reserved_nothing_is_not_the_victim() -> None:
    """Reserving nothing is not reserving zero.

    Its whole footprint would count as excess, so it would outrank every
    genuinely over-committed tenant and be stopped every pass, told it was
    furthest above a request it never made. The runtime reads the same value as
    "apply no limits at all".
    """
    stopped: list[tuple[str, StopContainerReason]] = []
    result = _watcher(
        pressure=90.0,
        residents=[
            ResidentContainer("unreserved", 101, reserved_mib=0),
            ResidentContainer("over", 102, reserved_mib=1024),
        ],
        current_mib={101: 16384, 102: 2048},
        stopped=stopped,
    ).run_once()

    assert result.evicted_container_id == "over"
    assert stopped == [("over", StopContainerReason.MemoryEvicted)]


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
            ResidentContainer("a", 201, reserved_mib=1024),
            ResidentContainer("b", 202, reserved_mib=1024),
        ],
        current_mib={201: 8192, 202: 4096},
        stopped=stopped,
        clock=clock,
    )

    assert watcher.run_once().evicted
    clock[0] = 2.0
    assert not watcher.run_once().evicted
    clock[0] = 30.0
    assert watcher.run_once().evicted
    assert len(stopped) == 2
