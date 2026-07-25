from __future__ import annotations

import os
import secrets
import time

from lazycloud import App, Image

APP_NAME = os.getenv("LAZYCLOUD_E2E_APP", f"shell_ticket_{secrets.token_hex(6)}")
app = App(APP_NAME)


@app.function(
    name="hold-ticket-target",
    image=Image(python_version="3.12"),
    cpu=0.25,
    memory="128Mi",
)
def hold_ticket_target(duration_seconds: float) -> str:
    time.sleep(duration_seconds)
    return "expired"
