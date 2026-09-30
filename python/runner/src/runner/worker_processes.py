from __future__ import annotations

import threading
import time
from collections.abc import Sequence
from multiprocessing import Process


def stop_worker_processes(
    shutdown: threading.Event,
    processes: Sequence[Process],
    *,
    grace_seconds: float = 5.0,
) -> None:
    shutdown.set()
    for process in processes:
        if process.is_alive():
            process.terminate()
    deadline = time.monotonic() + grace_seconds
    for process in processes:
        process.join(timeout=max(deadline - time.monotonic(), 0.0))
    for process in processes:
        if process.is_alive():
            process.kill()
            process.join(timeout=1)
