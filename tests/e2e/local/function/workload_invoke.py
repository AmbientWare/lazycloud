from __future__ import annotations

import secrets

from lazycloud import App, Image

APP_NAME = f"function_invoke_{secrets.token_hex(6)}"
app = App(APP_NAME)


@app.function(name="square", image=Image(python_version="3.12"), cpu=0.25, memory="128Mi")
def square(value: int) -> int:
    return value * value
