from __future__ import annotations

import secrets
import time

from lazycloud import App, Image

APP_NAME = f"function_cancel_rerun_{secrets.token_hex(6)}"
app = App(APP_NAME)


@app.function(name="delayed-square", image=Image(python_version="3.12"), cpu=0.25, memory="128Mi")
def delayed_square(value: int, delay_seconds: float) -> int:
    time.sleep(delay_seconds)
    return value * value
