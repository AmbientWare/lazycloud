from __future__ import annotations

import secrets

from lazycloud import App, Image

APP_NAME = f"function_retry_{secrets.token_hex(6)}"
app = App(APP_NAME)


@app.function(
    name="intentional-failure",
    image=Image(python_version="3.12"),
    cpu=0.25,
    memory="128Mi",
    retries=1,
)
def intentional_failure(marker: str) -> None:
    raise RuntimeError(f"intentional retry scenario failure: {marker}")
