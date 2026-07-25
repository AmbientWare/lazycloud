from __future__ import annotations

import secrets
from dataclasses import dataclass

from lazycloud import App, Image

APP_NAME = f"function_serialization_{secrets.token_hex(6)}"
app = App(APP_NAME)


@dataclass(frozen=True, slots=True)
class OpaqueNumber:
    value: int


@app.function(
    name="opaque-square",
    image=Image(python_version="3.12"),
    cpu=0.25,
    memory="128Mi",
)
def opaque_square(value: OpaqueNumber) -> OpaqueNumber:
    return OpaqueNumber(value.value * value.value)
