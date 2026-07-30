"""One Function that bursts past its cpu request and then holds real disk."""

from __future__ import annotations

import secrets

from lazycloud import App

REQUESTED_CORES = 0.125
REQUESTED_MEMORY = "128Mi"

BURST_PROCESSES = 8
BURST_SECONDS = 4.0
HOLD_MIB = 256
HOLD_SECONDS = 20.0

APP_NAME = f"e2e_container_usage_{secrets.token_hex(6)}"
app = App(APP_NAME)


@app.function(
    name="metered-workload",
    cpu=REQUESTED_CORES,
    memory=REQUESTED_MEMORY,
    disk="2Gi",
    timeout_seconds=300,
)
def metered_workload(
    processes: int,
    burst_seconds: float,
    hold_mib: int,
    hold_seconds: float,
) -> dict[str, float]:
    """Burn cpu past the request, then hold disk long enough to be sampled."""
    import os
    import resource
    import time

    def spin(duration: float) -> None:
        end = time.monotonic() + duration
        while time.monotonic() < end:
            pass

    children: list[int] = []
    for _ in range(processes):
        pid = os.fork()
        if pid == 0:
            spin(burst_seconds)
            os._exit(0)
        children.append(pid)
    for pid in children:
        os.waitpid(pid, 0)
    consumed = resource.getrusage(resource.RUSAGE_CHILDREN)

    hold_path = "/lazycloud-e2e-held"
    with open(hold_path, "wb") as handle:
        handle.write(b"\0" * (hold_mib * 1024 * 1024))
        handle.flush()
        os.fsync(handle.fileno())
    held_bytes = os.path.getsize(hold_path)
    time.sleep(hold_seconds)
    os.remove(hold_path)
    return {
        "cpu_core_seconds": round(consumed.ru_utime + consumed.ru_stime, 3),
        "held_bytes": held_bytes,
        "held_seconds": hold_seconds,
    }
