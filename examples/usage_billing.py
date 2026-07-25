from __future__ import annotations

import os
import time
from pathlib import Path

from lazycloud import App, Image

app = App("usage_billing_smoke")
image = Image(python_version="3.12")


def _exercise_disk_io(value: int) -> None:
    path = Path(f"/var/tmp/lazycloud-usage-{os.getpid()}-{value}.bin")
    try:
        with path.open("wb") as handle:
            handle.write(b"u" * 4 * 1024 * 1024)
            handle.flush()
            os.fsync(handle.fileno())
    finally:
        path.unlink(missing_ok=True)


@app.function(
    name="tracked-function",
    image=image,
    authorized=False,
)
def tracked_function(value: int = 1) -> dict[str, int]:
    _exercise_disk_io(value)
    time.sleep(1.2)
    return {"value": value * 2}


@app.endpoint(
    name="tracked-endpoint",
    route="/usage-endpoint",
    methods=["POST"],
    image=image,
    authorized=False,
    keep_warm=1,
)
def tracked_endpoint(value: int = 1) -> dict[str, int]:
    _exercise_disk_io(value)
    time.sleep(1.2)
    return {"value": value + 1}


@app.task_queue(
    name="tracked-task-queue",
    image=image,
    authorized=False,
    workers=1,
    keep_warm_seconds=1,
)
def tracked_task_queue(value: int = 1) -> dict[str, int]:
    _exercise_disk_io(value)
    time.sleep(1.2)
    return {"value": value + 3}


__all__ = ["app", "tracked_endpoint", "tracked_function", "tracked_task_queue"]
