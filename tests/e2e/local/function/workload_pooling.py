from __future__ import annotations

import os
import secrets

from lazycloud import App, Image

APP_NAME = f"function_pooling_{secrets.token_hex(6)}"

app = App(APP_NAME)

# Process state, which is the whole subject: it only survives between calls if
# the same interpreter serves them. A volume would prove something weaker and
# add a flush to reason about.
STARTS = 0
CALLS = 0


def record_start(context: object) -> None:
    """Stand in for the expensive setup `on_start` exists for.

    Takes the lifecycle context because that is what a hook is called with; a
    zero-argument hook raises `TypeError` before its body runs.
    """

    global STARTS
    STARTS += 1


@app.function(
    name="identify",
    image=Image(python_version="3.12"),
    cpu=0.25,
    memory="128Mi",
    on_start=record_start,
)
def identify(value: int) -> dict[str, str | int]:
    """Report who ran this call, and what that process has already done."""

    global CALLS
    CALLS += 1
    return {
        "value": value * value,
        "container_id": os.environ.get("CONTAINER_ID", ""),
        "starts": STARTS,
        "calls": CALLS,
    }
