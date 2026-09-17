from __future__ import annotations

import inspect
from collections.abc import Callable, Mapping
from typing import Any

from pydantic import TypeAdapter
from pydantic import ValidationError as PydanticValidationError
from shared.callables import bind_arguments, prepare_callable_arguments
from shared.function_payloads import FunctionPayloadEncoding

from lazycloud.abstractions.metadata import SchemaInput, schema_metadata
from lazycloud.schema import OutputValidationError, Schema, ValidationError


def serialize_result(target: Callable[..., Any], value: Any, outputs: SchemaInput) -> Any:
    if outputs is None:
        return value
    if inspect.isawaitable(value):
        return _serialize_awaited_result(target, value, outputs)
    schema = outputs if isinstance(outputs, Schema) else Schema.from_dict(schema_metadata(outputs))
    try:
        if isinstance(value, Mapping):
            values = TypeAdapter[dict[str, Any]](dict[str, Any]).validate_python(value)
            return schema.dump(values)
        if (
            len(schema.fields) == 1
            and inspect.signature(target).return_annotation is not inspect.Signature.empty
        ):
            name = next(iter(schema.fields))
            return schema.dump({name: value})[name]
        raise ValidationError("expected an object matching the output schema")
    except ValidationError as exc:
        raise OutputValidationError(f"invalid output: {exc}") from None
    except PydanticValidationError as exc:
        detail = "; ".join(error["msg"] for error in exc.errors(include_input=False))
        raise OutputValidationError(f"invalid output: {detail}") from None


async def _serialize_awaited_result(
    target: Callable[..., Any], value: Any, outputs: SchemaInput
) -> Any:
    return serialize_result(target, await value, outputs)


def encode_arguments(
    target: Callable[..., Any],
    args: tuple[Any, ...],
    kwargs: Mapping[str, Any],
    inputs: SchemaInput,
) -> tuple[tuple[Any, ...], dict[str, Any]]:
    if inputs is None:
        return args, dict(kwargs)
    schema = inputs if isinstance(inputs, Schema) else Schema.from_dict(schema_metadata(inputs))
    bound = inspect.signature(target).bind_partial(*args, **kwargs)
    for name, value in bound.arguments.items():
        if name in schema.fields:
            bound.arguments[name] = schema.fields[name].encode_input(value)
    return bound.args, bound.kwargs


def prepare_arguments(
    target: Callable[..., Any],
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
    inputs: SchemaInput,
    *,
    encoding: FunctionPayloadEncoding = FunctionPayloadEncoding.Json,
) -> tuple[tuple[Any, ...], dict[str, Any]]:
    if inputs is None:
        return prepare_callable_arguments(target, args, kwargs, encoding=encoding)
    schema = inputs if isinstance(inputs, Schema) else Schema.from_dict(schema_metadata(inputs))
    bound = bind_arguments(target, args, kwargs)
    bound.apply_defaults()
    bound.arguments.update(schema.validate(bound.arguments))
    return bound.args, bound.kwargs
