from __future__ import annotations

from dataclasses import fields, is_dataclass
from typing import Protocol, runtime_checkable

from pydantic import JsonValue, TypeAdapter

_JSON_VALUE = TypeAdapter[JsonValue](JsonValue)
_JSON_OBJECT = TypeAdapter(dict[str, JsonValue])


@runtime_checkable
class _ModelDumpable(Protocol):
    def model_dump(self, *, mode: str) -> object: ...


def resource_payload(value: object) -> object:
    if isinstance(value, _ModelDumpable):
        return value.model_dump(mode="json")
    if is_dataclass(value) and not isinstance(value, type):
        return {
            field.name: getattr(value, field.name)
            for field in fields(value)
            if not field.name.startswith("_")
        }
    return value


def validate_json_value(value: object) -> JsonValue:
    """Validate an untrusted Python value at an SDK JSON boundary."""

    return _JSON_VALUE.validate_python(value)


def validate_json_object(value: object) -> dict[str, JsonValue]:
    """Validate an untrusted Python value as an object with string keys."""

    return _JSON_OBJECT.validate_python(value)


def parse_json_value(value: str | bytes | bytearray) -> JsonValue:
    """Decode and validate one JSON document."""

    return _JSON_VALUE.validate_json(value)


def parse_json_object(value: str | bytes | bytearray) -> dict[str, JsonValue]:
    """Decode and validate one JSON object document."""

    return _JSON_OBJECT.validate_json(value)


__all__ = [
    "JsonValue",
    "parse_json_object",
    "parse_json_value",
    "resource_payload",
    "validate_json_object",
    "validate_json_value",
]
