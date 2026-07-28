from __future__ import annotations

import base64
from pathlib import Path

import pytest
from lazycloud.abstractions.artifact import ArtifactNotSavedError
from lazycloud.schema import Image

PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGA"
    "WjR9awAAAABJRU5ErkJggg=="
)


class UrlResponse:
    def __init__(self, data: bytes) -> None:
        self.data = data

    def __enter__(self) -> UrlResponse:
        return self

    def __exit__(self, *args: object) -> None:
        _ = args

    def read(self) -> bytes:
        return self.data


class SerializableImageValue:
    format = "PNG"
    size = (1, 1)
    mode = "RGB"

    def save(self, path: str | Path, *, format: str, **params: object) -> None:
        _ = format, params
        Path(path).write_bytes(PNG_1X1)

    def convert(self, mode: str) -> SerializableImageValue:
        self.mode = mode
        return self


def test_image_schema_accepts_structural_image_objects_without_pillow() -> None:
    value = SerializableImageValue()
    field = Image(allowed_formats=["PNG"])

    assert field.validate(value) is value
    with pytest.raises(ArtifactNotSavedError):
        field.dump(value)
