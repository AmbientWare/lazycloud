"""One Function pinned to connected AWS whose requests are a fraction of its use."""

from __future__ import annotations

import secrets

from shared.compute_policy import ComputePlacementTarget

from lazycloud import App, Image

REQUESTED_CORES = 0.125
REQUESTED_MEMORY = "128Mi"
REQUESTED_DISK = "2Gi"
REQUESTED_DISK_BYTES = 2 * 1024**3

BURST_PROCESSES = 8
BURST_SECONDS = 3.0
ALLOCATE_MIB = 512
FILL_CHUNK_MIB = 32

APP_NAME = f"e2e_aws_bounding_{secrets.token_hex(6)}"


def _bounded_workload(
    processes: int,
    burst_seconds: float,
    allocate_mib: int,
    chunk_mib: int,
) -> dict[str, float]:
    """Burst past every request from inside the container and report what happened.

    Sizes arrive as call arguments so the body depends on nothing but its own
    arguments once it is serialized onto a remote worker.
    """
    import contextlib
    import os
    import resource
    import time

    def spin(duration: float) -> None:
        end = time.monotonic() + duration
        while time.monotonic() < end:
            pass

    started = time.monotonic()
    children: list[int] = []
    for _ in range(processes):
        pid = os.fork()
        if pid == 0:
            spin(burst_seconds)
            os._exit(0)
        children.append(pid)
    for pid in children:
        os.waitpid(pid, 0)
    wall_seconds = time.monotonic() - started
    consumed = resource.getrusage(resource.RUSAGE_CHILDREN)
    cpu_core_seconds = consumed.ru_utime + consumed.ru_stime

    block = bytearray(allocate_mib * 1024 * 1024)
    for offset in range(0, len(block), 4096):
        block[offset] = 1
    allocated_mib = len(block) // (1024 * 1024)
    del block

    root = os.statvfs("/")
    root_total_bytes = root.f_blocks * root.f_frsize
    chunk = b"\0" * (chunk_mib * 1024 * 1024)
    fill_path = "/lazycloud-e2e-fill"
    written_bytes = 0
    write_errno = 0
    try:
        with open(fill_path, "wb") as handle:
            while written_bytes < root_total_bytes * 2:
                handle.write(chunk)
                handle.flush()
                written_bytes += len(chunk)
    except OSError as exc:
        write_errno = exc.errno or 0
    finally:
        with contextlib.suppress(OSError):
            os.remove(fill_path)

    return {
        "wall_seconds": round(wall_seconds, 3),
        "cpu_core_seconds": round(cpu_core_seconds, 3),
        "effective_cores": round(cpu_core_seconds / wall_seconds, 3) if wall_seconds else 0.0,
        "allocated_mib": allocated_mib,
        "root_total_bytes": root_total_bytes,
        "written_bytes": written_bytes,
        "write_errno": write_errno,
    }


app = App(APP_NAME)
bounded_workload = app.function(
    _bounded_workload,
    name="aws-bounded-workload",
    image=Image(python_version="3.12"),
    cpu=REQUESTED_CORES,
    memory=REQUESTED_MEMORY,
    disk=REQUESTED_DISK,
    timeout_seconds=600,
    placement=ComputePlacementTarget.Aws,
)
