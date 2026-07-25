from __future__ import annotations

from pydantic import JsonValue, TypeAdapter

_JSON_MAPPING_ADAPTER = TypeAdapter(dict[str, JsonValue])
_JSON_LIST_ADAPTER = TypeAdapter(list[JsonValue])


def require_json_object(value: JsonValue, *, name: str) -> dict[str, JsonValue]:
    assert isinstance(value, dict), f"{name} must be a JSON object"
    return value


def require_json_list(value: JsonValue, *, name: str) -> list[JsonValue]:
    assert isinstance(value, list), f"{name} must be a JSON array"
    return value


def require_str(value: JsonValue, *, name: str) -> str:
    assert isinstance(value, str), f"{name} must be a string"
    return value


def require_bool(value: JsonValue, *, name: str) -> bool:
    assert type(value) is bool, f"{name} must be a boolean"
    return value


def require_int(value: JsonValue, *, name: str) -> int:
    assert isinstance(value, int) and not isinstance(value, bool), f"{name} must be an integer"
    return value
