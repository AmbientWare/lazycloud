from __future__ import annotations

import pickle
from typing import Any

from pydantic import TypeAdapter, ValidationError
from shared.function_payloads import (
    FunctionPayloadEncoding,
    FunctionResultPayload,
)

FUNCTION_RESULT_ADAPTER = TypeAdapter[FunctionResultPayload](FunctionResultPayload)


class FunctionResultDecodeError(ValueError):
    pass


def decode_function_result(value: object) -> Any:
    payload = parse_function_result(value)
    try:
        if payload.encoding is FunctionPayloadEncoding.Json:
            return payload.value
        encoded = payload.bytes_value()
        return pickle.loads(encoded) if encoded else None
    except (
        AttributeError,
        EOFError,
        ImportError,
        IndexError,
        pickle.UnpicklingError,
        TypeError,
        ValueError,
    ) as exc:
        raise FunctionResultDecodeError("invalid function result payload") from exc


def parse_function_result(value: object) -> FunctionResultPayload:
    try:
        return FUNCTION_RESULT_ADAPTER.validate_python(value)
    except ValidationError as exc:
        raise FunctionResultDecodeError("invalid function result payload") from exc


__all__ = [
    "FunctionResultDecodeError",
    "decode_function_result",
    "parse_function_result",
]
