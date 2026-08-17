from __future__ import annotations

import os
import secrets
import time

from shared.autoscaling import QueueDepthAutoscaler

from lazycloud import App, Image

APP_NAME = f"function_scaling_{secrets.token_hex(6)}"
BURST = 6
HOLD_SECONDS = 4.0

app = App(APP_NAME)


@app.function(
    name="burst",
    image=Image(python_version="3.12"),
    cpu=0.25,
    memory="128Mi",
    autoscaler=QueueDepthAutoscaler(max_containers=BURST, tasks_per_container=1),
)
def burst(value: int) -> dict[str, str | int]:
    """Hold a container long enough that a backlog cannot drain through one."""

    time.sleep(HOLD_SECONDS)
    return {"value": value, "container_id": os.environ.get("CONTAINER_ID", "")}
