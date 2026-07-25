from __future__ import annotations

import secrets

from lazycloud import App, Image

APP_NAME = f"function_dependency_{secrets.token_hex(6)}"
app = App(APP_NAME)
image = Image(python_version="3.12")


@app.function(name="square", image=image, cpu=0.25, memory="128Mi")
def square(value: int) -> int:
    return value * value


@app.function(name="double", image=image, cpu=0.25, memory="128Mi")
def double(value: int) -> int:
    return value * 2
