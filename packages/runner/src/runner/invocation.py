from __future__ import annotations

import asyncio
import inspect
import io
import pickle
from collections.abc import Callable, Mapping
from contextlib import ExitStack
from typing import Any, get_type_hints

import cloudpickle
from pydantic import TypeAdapter, ValidationError
from pydantic.errors import PydanticSchemaGenerationError
from shared.schema import MediaOutput, MediaPublisher, SchemaCallable, ValueSchema
from shared.schema import ValidationError as SchemaValidationError


class OutputValidationError(ValueError):
    pass


def cloudpickle_bytes(value: Any) -> bytes:
    stream = io.BytesIO()
    pickler = cloudpickle.CloudPickler(stream)
    pickle.Pickler.dump(pickler, value)
    return stream.getvalue()


def callable_target(handler: Callable[..., Any]) -> Callable[..., Any]:
    target = getattr(handler, "func", handler)
    if not callable(target):
        msg = f"handler is not callable: {type(handler).__name__}"
        raise TypeError(msg)
    return target


def invoke_handler(
    handler: Callable[..., Any],
    args: tuple[Any, ...],
    kwargs: Mapping[str, Any],
    *,
    publish: MediaPublisher | None = None,
) -> Any:
    target = callable_target(handler)
    with ExitStack() as files:
        explicit_fields: frozenset[str] = frozenset()
        if isinstance(handler, SchemaCallable) and handler.inputs is not None:
            schema = ValueSchema.from_definition(handler.inputs)
            bound = schema.transform_arguments(
                target,
                args,
                kwargs,
                lambda field, value: field.validate_value(value, files=files),
            )
            args, kwargs = bound.args, bound.kwargs
            explicit_fields = frozenset(schema.fields)
        coerced_args, coerced_kwargs = coerce_arguments(
            target, args, dict(kwargs), explicit_fields=explicit_fields
        )
        result = target(*coerced_args, **coerced_kwargs)
        if inspect.isawaitable(result):
            result = asyncio.run(_await_any(result))
        if isinstance(handler, SchemaCallable) and handler.outputs is not None:
            try:
                return ValueSchema.from_definition(handler.outputs).dump_values(
                    result, publish=publish or _missing_publisher, files=files
                )
            except SchemaValidationError as exc:
                raise OutputValidationError(str(exc)) from None
        return result


def _missing_publisher(media: MediaOutput) -> str:
    raise RuntimeError("File and Image outputs require a task artifact publisher")


async def _await_any(value: Any) -> Any:
    return await value


def coerce_arguments(
    target: Callable[..., Any],
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
    *,
    explicit_fields: frozenset[str] = frozenset(),
) -> tuple[tuple[Any, ...], dict[str, Any]]:
    hints = _type_hints(target)
    if not hints:
        return args, kwargs
    signature = inspect.signature(target)
    bound = signature.bind(*args, **kwargs)
    for name, value in list(bound.arguments.items()):
        if name in explicit_fields:
            continue
        annotation = hints.get(name)
        if annotation is None or annotation is Any:
            continue
        parameter = signature.parameters.get(name)
        if parameter is None:
            continue
        if parameter.kind is inspect.Parameter.VAR_POSITIONAL:
            bound.arguments[name] = tuple(_coerce_value(annotation, item) for item in value)
            continue
        if parameter.kind is inspect.Parameter.VAR_KEYWORD:
            bound.arguments[name] = {
                key: item if key in explicit_fields else _coerce_value(annotation, item)
                for key, item in value.items()
            }
            continue
        bound.arguments[name] = _coerce_value(annotation, value)
    return bound.args, bound.kwargs


def _coerce_value(annotation: Any, value: Any) -> Any:
    try:
        return TypeAdapter(annotation).validate_python(value)
    except (ValidationError, PydanticSchemaGenerationError):
        # An annotation Pydantic cannot build or satisfy leaves the argument as
        # the caller sent it; the handler's own signature is the next check.
        return value


def _type_hints(target: Callable[..., Any]) -> dict[str, Any]:
    try:
        return get_type_hints(target, include_extras=True)
    except (NameError, TypeError):
        # A forward reference that does not resolve in this process still has a
        # usable raw annotation.
        return dict(getattr(target, "__annotations__", {}))


__all__ = ["callable_target", "cloudpickle_bytes", "coerce_arguments", "invoke_handler"]
