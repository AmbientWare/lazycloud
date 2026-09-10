from __future__ import annotations

import os
import secrets
import sys
import time

from lazycloud import App, Image, current_task_id

APP_NAME = f"function_logs_{secrets.token_hex(6)}"
app = App(APP_NAME)


@app.function(
    image=Image(python_version="3.12"),
    cpu=0.5,
    memory="256Mi",
    concurrency=2,
    in_process=True,
    keep_warm=30,
)
def emit(value: int) -> dict[str, str | int | float]:
    started = time.monotonic()
    for index in range(64):
        print(f"{value}:stdout:{index}")
    print(f"{value}:stderr", file=sys.stderr)
    sys.stdout.write(f"{value}:partial")
    emitted = time.monotonic()
    time.sleep(0.3)
    return {
        "value": value,
        "task_id": current_task_id(),
        "pid": os.getpid(),
        "started": started,
        "finished": time.monotonic(),
        "emit_seconds": emitted - started,
    }
