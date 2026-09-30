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


def decode_function_result(
    value: object, *, expected_encoding: FunctionPayloadEncoding | None = None
) -> Any:
    payload = parse_function_result(value)
    if expected_encoding is not None and payload.encoding is not expected_encoding:
        raise FunctionResultDecodeError(
            f"expected a {expected_encoding.value} result, received {payload.encoding.value}"
        )
    try:
        if payload.encoding is FunctionPayloadEncoding.Json:
            return payload.value
        encoded = payload.bytes_value()
        return pickle.loads(encoded)
    except ImportError as exc:
        raise FunctionResultDecodeError(
            f"cannot load Python result: missing dependency {exc.name or 'module'!r}; "
            "install the function's dependencies in the calling environment"
        ) from exc
    except AttributeError as exc:
        raise FunctionResultDecodeError(
            "cannot load Python result: its class or attribute is unavailable; "
            "use matching user code and dependency versions in the calling environment"
        ) from exc
    except (
        EOFError,
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
