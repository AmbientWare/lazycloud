from __future__ import annotations

import asyncio
import inspect
import io
import pickle
from collections.abc import Callable
from typing import Any, get_type_hints

import cloudpickle
from pydantic import TypeAdapter


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
    /,
    *args: Any,
    **kwargs: Any,
) -> Any:
    target = callable_target(handler)
    coerced_args, coerced_kwargs = coerce_arguments(target, args, kwargs)
    result = target(*coerced_args, **coerced_kwargs)
    if inspect.isawaitable(result):
        return asyncio.run(_await_any(result))
    return result


async def _await_any(value: Any) -> Any:
    return await value


def coerce_arguments(
    target: Callable[..., Any],
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
) -> tuple[tuple[Any, ...], dict[str, Any]]:
    hints = _type_hints(target)
    if not hints:
        return args, kwargs
    signature = inspect.signature(target)
    bound = signature.bind(*args, **kwargs)
    for name, value in list(bound.arguments.items()):
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
                key: _coerce_value(annotation, item) for key, item in value.items()
            }
            continue
        bound.arguments[name] = _coerce_value(annotation, value)
    return bound.args, bound.kwargs


def _coerce_value(annotation: Any, value: Any) -> Any:
    try:
        return TypeAdapter(annotation).validate_python(value)
    except Exception:
        return value


def _type_hints(target: Callable[..., Any]) -> dict[str, Any]:
    try:
        return get_type_hints(target, include_extras=True)
    except Exception:
        return dict(getattr(target, "__annotations__", {}))


__all__ = ["callable_target", "cloudpickle_bytes", "coerce_arguments", "invoke_handler"]
