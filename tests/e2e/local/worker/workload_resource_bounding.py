"""One Function whose requests are a fraction of what it goes on to consume."""

from __future__ import annotations

import secrets

from lazycloud import App

# A request reserves capacity; it is not a cap. Every one of these is exceeded
# by the workload below, and the disk ceiling is the only bound that must hold.
REQUESTED_CORES = 0.125
REQUESTED_MEMORY = "128Mi"
REQUESTED_DISK = "2Gi"
REQUESTED_DISK_BYTES = 2 * 1024**3
DEFAULT_DISK_BYTES = 100 * 1024**3

BURST_PROCESSES = 8
BURST_SECONDS = 3.0
ALLOCATE_MIB = 512
FILL_CHUNK_MIB = 32

# Eight busy processes against an eighth of a core. One whole core is already
# eight times the request while staying far below both the burst ceiling and
# what any host running this stack can supply, so the threshold does not depend
# on the host's core count.
MINIMUM_BURST_CORES = 1.0

APP_NAME = f"e2e_bounding_{secrets.token_hex(6)}"
app = App(APP_NAME)


@app.function(
    name="bounded-workload",
    cpu=REQUESTED_CORES,
    memory=REQUESTED_MEMORY,
    disk=REQUESTED_DISK,
    timeout_seconds=300,
)
def bounded_workload(
    processes: int,
    burst_seconds: float,
    allocate_mib: int,
    chunk_mib: int,
) -> dict[str, float]:
    """Burst past every request from inside the container and report what happened.

    Sizes arrive as call arguments so the body depends on nothing but its own
    arguments once it is serialized into the container.
    """
    import contextlib
    import os
    import resource
    import time

    def spin(duration: float) -> None:
        end = time.monotonic() + duration
        while time.monotonic() < end:
            pass

    # Processes, not threads: the GIL serialises pure-Python spinning to about
    # one core no matter what the cgroup allows.
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

    # Touch every page: an untouched allocation never reaches the memory cgroup.
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
            # Twice the visible root: reaching that bound means nothing stopped us.
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
