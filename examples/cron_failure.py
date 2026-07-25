"""Cron failure and retry acceptance app.

Deploy this app with a unique name, observe one Run reaching its configured
retry limit, then delete it after lifecycle verification.
"""

from __future__ import annotations

import os

from lazycloud import App, Image

APP_NAME_ENV = "LAZYCLOUD_CRON_FAILURE_APP_NAME"

app = App(os.getenv(APP_NAME_ENV, "cron_failure"))
image = Image(python_version="3.12")


@app.cron(
    "every 1m",
    name="failing-heartbeat",
    image=image,
    cpu=0.25,
    memory="128Mi",
    retries=1,
)
def failing_heartbeat() -> None:
    raise RuntimeError("intentional cron retry acceptance failure")


__all__ = ["app", "failing_heartbeat"]
