from __future__ import annotations

import os
import secrets
import time

from lazycloud import App, Image, current_task_id

APP_NAME = f"function_concurrency_{secrets.token_hex(6)}"
CONCURRENCY = 4
HOLD_SECONDS = 3.0

app = App(APP_NAME)


@app.function(
    name="hold",
    image=Image(python_version="3.12"),
    cpu=0.25,
    memory="128Mi",
    concurrency=CONCURRENCY,
)
def hold(value: int) -> dict[str, str | int | float]:
    """Occupy a worker long enough that serial execution is distinguishable.

    Reports the process and container that ran it. With one task per process the
    pid is what separates workers, and it is also what would expose a task being
    run by a worker that another task believes it owns.
    """

    started = time.monotonic()
    time.sleep(HOLD_SECONDS)
    return {
        "value": value * value,
        "pid": os.getpid(),
        "container_id": os.environ.get("CONTAINER_ID", ""),
        "task_id": current_task_id(),
        "held": time.monotonic() - started,
    }
