from __future__ import annotations

import secrets
import time

from lazycloud import App, Image

APP_NAME = f"function_backpressure_{secrets.token_hex(6)}"
MAX_PENDING = 3
HOLD_SECONDS = 8.0

app = App(APP_NAME)


@app.function(
    name="occupy",
    image=Image(python_version="3.12"),
    cpu=0.25,
    memory="128Mi",
    max_pending_tasks=MAX_PENDING,
)
def occupy(value: int) -> int:
    """Stay in flight long enough for the limit to be reached by spawning."""

    time.sleep(HOLD_SECONDS)
    return value
