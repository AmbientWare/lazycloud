from datetime import UTC, datetime, timedelta
from time import sleep

import pytest
from agent.capacity_shutdown import CapacityShutdown


class _Workers:
    def __init__(self) -> None:
        self.grace_periods: list[float] = []

    def gracefully_stop_all(self, *, grace_seconds: float) -> None:
        self.grace_periods.append(grace_seconds)

    def stop_all(self) -> None:
        self.grace_periods.append(0.0)


def test_shutdown_deadline_runs_independently_and_cannot_be_extended() -> None:
    workers = _Workers()
    shutdown = CapacityShutdown(workers)
    deadline = datetime.now(UTC) + timedelta(seconds=20.05)
    try:
        shutdown.arm(deadline)
        shutdown.arm(deadline + timedelta(minutes=1))
        for _ in range(100):
            if workers.grace_periods:
                break
            sleep(0.01)
        assert len(workers.grace_periods) == 1
        assert 0 < workers.grace_periods[0] <= 15
        shutdown.stop()
        assert len(workers.grace_periods) == 1
    finally:
        shutdown.close()


class _UnavailableWorkers(_Workers):
    def __init__(self) -> None:
        super().__init__()
        self.unavailable = True

    def gracefully_stop_all(self, *, grace_seconds: float) -> None:
        del grace_seconds
        raise RuntimeError("worker did not stop gracefully")

    def stop_all(self) -> None:
        if self.unavailable:
            raise RuntimeError("docker unavailable")
        super().stop_all()


def test_failed_shutdown_can_retry_and_force_worker_removal() -> None:
    workers = _UnavailableWorkers()
    shutdown = CapacityShutdown(workers)

    with pytest.raises(RuntimeError, match="docker unavailable"):
        shutdown.stop()

    workers.unavailable = False
    shutdown.stop()
    shutdown.stop()

    assert workers.grace_periods == [0.0]
