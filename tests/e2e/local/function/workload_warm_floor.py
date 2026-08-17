from __future__ import annotations

import os
import secrets

from shared.autoscaling import QueueDepthAutoscaler

from lazycloud import App, Image

APP_NAME = f"function_warm_floor_{secrets.token_hex(6)}"
FLOOR = 2

app = App(APP_NAME)


@app.function(
    name="floored",
    image=Image(python_version="3.12"),
    cpu=0.25,
    memory="128Mi",
    autoscaler=QueueDepthAutoscaler(min_containers=FLOOR, max_containers=4),
)
def floored(value: int) -> dict[str, str | int]:
    """Answer from whichever of the held containers takes the call."""

    return {"value": value, "container_id": os.environ.get("CONTAINER_ID", "")}
