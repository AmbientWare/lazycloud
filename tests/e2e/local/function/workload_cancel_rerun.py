from __future__ import annotations

import secrets
import time

from shared.autoscaling import QueueDepthAutoscaler

from lazycloud import App, Image

APP_NAME = f"function_cancel_rerun_{secrets.token_hex(6)}"
CONCURRENCY = 2

app = App(APP_NAME)


@app.function(
    name="delayed-square",
    image=Image(python_version="3.12"),
    cpu=0.25,
    memory="128Mi",
    concurrency=CONCURRENCY,
    # One container, two slots: the two calls are co-resident by construction,
    # which is what makes cancelling one of them a statement about the other.
    autoscaler=QueueDepthAutoscaler(max_containers=1, tasks_per_container=CONCURRENCY),
)
def delayed_square(value: int, delay_seconds: float) -> int:
    time.sleep(delay_seconds)
    return value * value
