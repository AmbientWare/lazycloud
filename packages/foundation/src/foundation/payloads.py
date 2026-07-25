from __future__ import annotations

from pydantic import JsonValue, TypeAdapter, ValidationError
from shared.bytes_transport import encode_bytes

_JSON_VALUE_ADAPTER = TypeAdapter[JsonValue](JsonValue)


def json_or_base64_payload(value: bytes) -> JsonValue:
    if not value:
        return None
    try:
        return _JSON_VALUE_ADAPTER.validate_json(value)
    except ValidationError:
        return {"value_base64": encode_bytes(value)}
