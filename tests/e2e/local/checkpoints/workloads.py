"""The two minimal workloads used by checkpoint continuity scenarios."""

from __future__ import annotations

import atexit
import os
import threading
import time
from uuid import uuid4

from lazycloud import App, Image

# The driving scenario exports the exact app name before importing this module;
# the container re-imports it only for module:function handler resolution, so a
# missing variable falls back to an inert default instead of failing in-container.
APP_NAME = os.environ.get("LAZYCLOUD_E2E_CHECKPOINT_APP", "e2e_checkpoint")
POD_PORT = 8091
app = App(APP_NAME)
image = Image(python_version="3.12")
_boot_id = str(uuid4())
_born_at_ns = time.time_ns()
_counter = 0
_lock = threading.Lock()
_stop = threading.Event()


def _advance() -> None:
    global _counter
    while not _stop.wait(0.05):
        with _lock:
            _counter += 1


def _state(label: str) -> dict[str, str | int]:
    with _lock:
        counter = _counter
    return {
        "label": label,
        "boot_id": _boot_id,
        "born_at_ns": _born_at_ns,
        "counter": counter,
        "pid": os.getpid(),
    }


threading.Thread(target=_advance, name="checkpoint-continuity", daemon=True).start()
atexit.register(_stop.set)


@app.endpoint(
    name="checkpoint-endpoint",
    route="/checkpoint/endpoint",
    methods=["POST"],
    image=image,
    keep_warm=120,
    checkpoint_enabled=True,
)
def endpoint_probe(label: str) -> dict[str, str | int]:
    return _state(label)


pod = app.pod(
    name="checkpoint-pod",
    image=image,
    command=["python3", "pod_server.py"],
    ports={"http": POD_PORT},
    keep_warm=120,
    checkpoint_enabled=True,
    checkpoint_readiness_path="/ready",
    checkpoint_readiness_port=POD_PORT,
)


__all__ = ["app", "endpoint_probe", "pod"]
