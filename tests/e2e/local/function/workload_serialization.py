from __future__ import annotations

import secrets

import numpy as np
from numpy.typing import NDArray

from lazycloud import App, Image

APP_NAME = f"function_serialization_{secrets.token_hex(6)}"
app = App(APP_NAME)
image = Image(python_version="3.12").add_python_packages(["numpy>=2,<3"])


class OpaqueNumber:
    def __init__(self, value: int = 9) -> None:
        self.value = value

    def square(self) -> OpaqueNumber:
        return OpaqueNumber(self.value * self.value)


@app.function(
    name="opaque-square",
    image=image,
    cpu=0.25,
    memory="256Mi",
)
def opaque_square(value: OpaqueNumber = OpaqueNumber()) -> OpaqueNumber:
    return value.square()


@app.function(image=image, cpu=0.25, memory="256Mi")
def matrix() -> NDArray[np.int64]:
    return np.arange(6, dtype=np.int64).reshape(2, 3)


@app.function(image=image, cpu=0.25, memory="256Mi")
def double_matrix(value: NDArray[np.int64]) -> NDArray[np.int64]:
    return value * 2
