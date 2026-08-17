from __future__ import annotations

import os
import secrets
import time

from shared.autoscaling import QueueDepthAutoscaler

from lazycloud import App, Image

APP_NAME = f"function_map_{secrets.token_hex(6)}"
INPUTS = 24
CONCURRENCY = 4
MAX_CONTAINERS = 3
HOLD_SECONDS = 1.0

app = App(APP_NAME)


@app.function(
    name="square-one",
    image=Image(python_version="3.12"),
    cpu=0.25,
    memory="128Mi",
    concurrency=CONCURRENCY,
    autoscaler=QueueDepthAutoscaler(
        max_containers=MAX_CONTAINERS,
        tasks_per_container=CONCURRENCY,
    ),
)
def square_one(value: int) -> dict[str, str | int | float]:
    """One input of the fan-out, held long enough to overlap with its neighbours."""

    time.sleep(HOLD_SECONDS)
    return {
        "value": value * value,
        "container_id": os.environ.get("CONTAINER_ID", ""),
        "pid": os.getpid(),
    }
