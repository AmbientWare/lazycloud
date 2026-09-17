from __future__ import annotations

import inspect
from collections.abc import Callable
from typing import Any, Protocol, get_type_hints, runtime_checkable

from pydantic import PydanticSchemaGenerationError, TypeAdapter, ValidationError

from shared.errors import InvalidInputError
from shared.function_payloads import FunctionPayloadEncoding


@runtime_checkable
class InvocationHandler(Protocol):
    def invoke_arguments(
        self, args: tuple[Any, ...], kwargs: dict[str, Any], *, encoding: FunctionPayloadEncoding
    ) -> Any: ...


def bind_arguments(
    target: Callable[..., Any], args: tuple[Any, ...], kwargs: dict[str, Any]
) -> inspect.BoundArguments:
    try:
        return inspect.signature(target).bind(*args, **kwargs)
    except TypeError as exc:
        raise InvalidInputError(str(exc)) from None


def prepare_callable_arguments(
    target: Callable[..., Any],
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
    *,
    encoding: FunctionPayloadEncoding,
) -> tuple[tuple[Any, ...], dict[str, Any]]:
    bound = bind_arguments(target, args, kwargs)
    if encoding is FunctionPayloadEncoding.Cloudpickle:
        return bound.args, bound.kwargs
    annotated = target if inspect.isroutine(target) else target.__call__
    hints = (
        get_type_hints(annotated, include_extras=True)
        if getattr(annotated, "__annotations__", None)
        else {}
    )
    for name, value in list(bound.arguments.items()):
        annotation = hints.get(name)
        if annotation is None or annotation is Any:
            continue
        try:
            adapter = TypeAdapter(annotation)
        except PydanticSchemaGenerationError as exc:
            raise InvalidInputError(
                f"argument {name} requires a Python object; "
                "call this function through the Python SDK"
            ) from exc
        parameter = bound.signature.parameters[name]
        try:
            if parameter.kind is inspect.Parameter.VAR_POSITIONAL:
                bound.arguments[name] = tuple(adapter.validate_python(item) for item in value)
            elif parameter.kind is inspect.Parameter.VAR_KEYWORD:
                bound.arguments[name] = {
                    key: adapter.validate_python(item) for key, item in value.items()
                }
            else:
                bound.arguments[name] = adapter.validate_python(value)
        except ValidationError as exc:
            detail = "; ".join(error["msg"] for error in exc.errors(include_input=False))
            raise InvalidInputError(f"invalid argument {name}: {detail}") from None
    return bound.args, bound.kwargs


__all__ = ["InvocationHandler", "bind_arguments", "prepare_callable_arguments"]
