"""One Function whose container is held across several metering windows.

Sized so the charge is unambiguous rather than so the work is interesting: a
whole core and a whole gibibyte for long enough that the worker closes many
usage windows, which is what makes the ledger's total large enough to read and
to compare against an invoice.
"""

from __future__ import annotations

import secrets

from lazycloud import App

REQUESTED_CORES = 1.0
REQUESTED_MEMORY = "1Gi"
HOLD_SECONDS = 45.0
"""Several worker metering windows, which close on a five-second sample."""

APP_NAME = f"e2e_billing_metered_{secrets.token_hex(6)}"
app = App(APP_NAME)


@app.function(
    name="metered-container",
    cpu=REQUESTED_CORES,
    memory=REQUESTED_MEMORY,
    timeout_seconds=300,
)
def metered_container(hold_seconds: float) -> dict[str, float]:
    """Hold the container, and report how long it was actually held for."""
    import time

    started = time.monotonic()
    time.sleep(hold_seconds)
    return {"held_seconds": round(time.monotonic() - started, 3)}
