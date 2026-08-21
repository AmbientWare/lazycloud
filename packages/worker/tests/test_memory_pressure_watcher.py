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
) -> WorkerMemoryPressureWatcher:
    return WorkerMemoryPressureWatcher(
        worker_cgroup_path="/sys/fs/cgroup/worker",
        residents=lambda: residents,
        read_pressure_percent=lambda _path: pressure,
        read_memory_current=lambda pid: current_mib[pid] * MIB,
        stop_container=lambda container_id, reason: stopped.append((container_id, reason)),
    )


def test_a_struggling_machine_loses_the_container_furthest_over_its_reservation() -> None:
    """One container goes, and it is not the largest one.

    The kernel would take the eight-gibibyte tenant sitting exactly inside what
    it reserved, because that is what scoring resident size does. Getting there
    first is the only way the container that actually grew is the one that
    stops.
    """
    stopped: list[tuple[str, StopContainerReason]] = []
    result = _watcher(
        pressure=60.0,
        residents=[
            ResidentContainer("honest", 101, reserved_mib=8192),
            ResidentContainer("leaker", 102, reserved_mib=1024),
        ],
        current_mib={101: 8192, 102: 3072},
        stopped=stopped,
    ).run_once()

    assert result.evicted_container_id == "leaker"
    assert stopped == [("leaker", StopContainerReason.MemoryEvicted)]


def test_a_coping_machine_loses_nothing() -> None:
    stopped: list[tuple[str, StopContainerReason]] = []
    result = _watcher(
        pressure=1.0,
        residents=[ResidentContainer("leaker", 102, reserved_mib=1024)],
        current_mib={102: 8192},
        stopped=stopped,
    ).run_once()

    assert not result.evicted
    assert stopped == []


def test_nothing_is_stopped_when_every_container_is_inside_its_reservation() -> None:
    """Pressure with no one over their request is not theirs to answer for.

    Something this watcher does not place is using the machine, and stopping a
    tenant that kept its side of the bargain would be arbitrary.
    """
    stopped: list[tuple[str, StopContainerReason]] = []
    result = _watcher(
        pressure=90.0,
        residents=[
            ResidentContainer("a", 301, reserved_mib=4096),
            ResidentContainer("b", 302, reserved_mib=4096),
        ],
        current_mib={301: 4096, 302: 100},
        stopped=stopped,
    ).run_once()

    assert not result.evicted
    assert stopped == []
    assert result.considered == 2


def test_only_one_container_goes_per_pass() -> None:
    """Reclaim needs time to show up in the pressure reading.

    Acting on every container over its reservation at once would empty a machine
    that one departure would have settled.
    """
    stopped: list[tuple[str, StopContainerReason]] = []
    _watcher(
        pressure=90.0,
        residents=[
            ResidentContainer("over-one", 201, reserved_mib=1024),
            ResidentContainer("over-two", 202, reserved_mib=1024),
        ],
        current_mib={201: 4096, 202: 8192},
        stopped=stopped,
    ).run_once()

    assert len(stopped) == 1


def test_a_container_is_invisible_until_it_reports_a_sandbox() -> None:
    """A pid arrives after registration, and nothing works until it does.

    A long-running container has no sandbox when it is registered and reports
    none again until it exits. Left at zero it is filtered out of every pass, so
    the machine fills with containers the watcher cannot see and the kernel makes
    the decision after all.
    """
    stopped: list[tuple[str, StopContainerReason]] = []
    before = [ResidentContainer("leaker", 0, reserved_mib=1024)]
    after = [ResidentContainer("leaker", 102, reserved_mib=1024)]

    assert (
        not _watcher(
            pressure=90.0,
            residents=before,
            current_mib={102: 8192},
            stopped=stopped,
        )
        .run_once()
        .evicted
    )
    assert stopped == []

    assert (
        _watcher(
            pressure=90.0,
            residents=after,
            current_mib={102: 8192},
            stopped=stopped,
        )
        .run_once()
        .evicted
    )
