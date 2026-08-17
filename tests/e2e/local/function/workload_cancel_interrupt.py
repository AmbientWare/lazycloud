from __future__ import annotations

import secrets
import time

from lazycloud import App, Image

APP_NAME = f"function_cancel_interrupt_{secrets.token_hex(6)}"
HOLD_SECONDS = 20.0
AFTER_MARKER = "reached-the-far-side"

app = App(APP_NAME)


@app.function(name="two-phase", image=Image(python_version="3.12"), cpu=0.25, memory="128Mi")
def two_phase() -> str:
    """Announce itself, hold, then announce again.

    The second announcement is the whole point: it is written only by work that
    was still running long after the caller cancelled it.
    """

    print("started", flush=True)
    time.sleep(HOLD_SECONDS)
    print(AFTER_MARKER, flush=True)
    return AFTER_MARKER
