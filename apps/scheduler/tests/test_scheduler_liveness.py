from __future__ import annotations

import threading
from pathlib import Path

from scheduler.reconciliation import SchedulerRunResult
from scheduler_app.loops import run_loop
from shared.process_liveness import HeartbeatFile


def test_a_loop_beats_only_after_a_pass_it_finished() -> None:
    """The heartbeat has to mean a pass completed, not that one was attempted.

    Stamped before the work, it stays fresh while the pass beneath it is wedged,
    which is the failure an operator most needs the file to show. That is what
    it did when one loop ran everything and beat at the top of the tick.
    """

    beats: list[int] = []
    stop = threading.Event()
    attempts = 0

    def failing_pass() -> SchedulerRunResult:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("first pass fails")
        stop.set()
        return SchedulerRunResult()

    run_loop(
        name="capacity",
        stop=stop,
        interval_seconds=0.0,
        pass_once=failing_pass,
        beat=lambda: beats.append(1),
    )

    assert attempts == 2
    assert len(beats) == 1


def test_one_loop_failing_does_not_stop_the_others(tmp_path: Path) -> None:
    """A loop that cannot reach its dependency must not take placement with it.

    Backoff is per loop for this reason. One shared counter meant a housekeeping
    pass that could not reach Stripe throttled the loop a caller is waiting on,
    which is the coupling the split exists to remove.
    """

    stop = threading.Event()
    healthy_file = tmp_path / "healthy"
    healthy_beat = HeartbeatFile(healthy_file).beat
    healthy_passes = 0

    def healthy_pass() -> SchedulerRunResult:
        nonlocal healthy_passes
        healthy_passes += 1
        if healthy_passes >= 3:
            stop.set()
        return SchedulerRunResult()

    def broken_pass() -> SchedulerRunResult:
        raise RuntimeError("dependency is unreachable")

    broken = threading.Thread(
        target=run_loop,
        kwargs={
            "name": "housekeeping",
            "stop": stop,
            "interval_seconds": 0.0,
            "pass_once": broken_pass,
        },
        daemon=True,
    )
    broken.start()
    run_loop(
        name="placement",
        stop=stop,
        interval_seconds=0.0,
        pass_once=healthy_pass,
        beat=healthy_beat,
    )
    broken.join(timeout=5)

    assert healthy_passes >= 3
    assert healthy_file.exists()
