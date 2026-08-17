from __future__ import annotations

import os
import secrets
import threading
import time

from lazycloud import App, Image, current_task_id

APP_NAME = f"function_in_process_{secrets.token_hex(6)}"
CONCURRENCY = 8
INVOCATIONS = 50
HOLD_SECONDS = 0.4

app = App(APP_NAME)

# Stands in for the thing this mode exists to share. A model in VRAM cannot be
# loaded per slot on one card; what makes that work is that `on_start` runs once
# and every slot afterwards sees the same object. Its identity is the evidence —
# a process per slot would report a different one from each.
_LOADED: dict[str, object] = {}


def load_once(context: object) -> None:
    """Load the shared object once, before any slot serves a call.

    Takes the lifecycle context because that is what a hook is called with; a
    zero-argument hook raises `TypeError` before its body runs, and `on_start`
    failing stops the container rather than one invocation.
    """

    del context
    _LOADED["model"] = object()
    _LOADED["loaded_by"] = os.getpid()


@app.function(
    name="shared",
    image=Image(python_version="3.12"),
    cpu=0.25,
    memory="128Mi",
    concurrency=CONCURRENCY,
    in_process=True,
    on_start=load_once,
)
def shared(value: int) -> dict[str, str | int | float]:
    """Report who ran this call, and which loaded object it was served by.

    The task id is read through the SDK rather than the environment because
    there is no per-process answer any more: several of these run in one
    interpreter, and an environment variable would hold whichever of them wrote
    it last.
    """

    started = time.monotonic()
    time.sleep(HOLD_SECONDS)
    finished = time.monotonic()
    return {
        "value": value * value,
        "pid": os.getpid(),
        "thread": threading.get_ident(),
        "model_id": id(_LOADED.get("model")),
        "loaded_by": int(_LOADED.get("loaded_by") or 0),
        "container_id": os.environ.get("CONTAINER_ID", ""),
        "task_id": current_task_id(),
        # One interpreter means one clock, so these are directly comparable
        # between invocations. That is what makes overlap measurable from the
        # answers rather than inferred from how long the whole run took.
        "started_at": started,
        "finished_at": finished,
        "held": finished - started,
    }
