"""One Function whose container costs more than a cent to run.

Sized by what has to be visible on an invoice rather than by the work: Stripe
charges whole cents, so usage worth a fraction of one is a line reading $0.00 and
an allowance that discounts nothing anybody can see. The shape and the hold below
are the smallest pair that clears a cent on the published rates while fitting
inside one worker's advertised capacity.
"""

from __future__ import annotations

import secrets

from lazycloud import App

REQUESTED_CORES = 3.0
REQUESTED_MEMORY = "5Gi"
HOLD_SECONDS = 420.0
"""Long enough that the priced total is worth whole cents, not a rounding remnant."""

APP_NAME = f"e2e_billing_billable_{secrets.token_hex(6)}"
app = App(APP_NAME)


@app.function(
    name="billable-container",
    cpu=REQUESTED_CORES,
    memory=REQUESTED_MEMORY,
    timeout_seconds=900,
)
def billable_container(hold_seconds: float) -> dict[str, float]:
    """Hold the container, and report how long it was actually held for."""
    import time

    started = time.monotonic()
    time.sleep(hold_seconds)
    return {"held_seconds": round(time.monotonic() - started, 3)}
