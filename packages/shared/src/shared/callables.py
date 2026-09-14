from __future__ import annotations

import inspect
from collections.abc import Callable
from typing import Any, Protocol, get_type_hints, runtime_checkable

from pydantic import TypeAdapter, ValidationError

from shared.errors import InvalidInputError


@runtime_checkable
class InvocationHandler(Protocol):
    def invoke_arguments(self, args: tuple[Any, ...], kwargs: dict[str, Any]) -> Any: ...


def bind_arguments(
    target: Callable[..., Any], args: tuple[Any, ...], kwargs: dict[str, Any]
) -> inspect.BoundArguments:
    try:
        return inspect.signature(target).bind(*args, **kwargs)
    except TypeError as exc:
        raise InvalidInputError(str(exc)) from None


def coerce_arguments(
    target: Callable[..., Any],
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
) -> tuple[tuple[Any, ...], dict[str, Any]]:
    annotated = target if inspect.isroutine(target) else target.__call__
    hints = (
        get_type_hints(annotated, include_extras=True)
        if getattr(annotated, "__annotations__", None)
        else {}
    )
    bound = bind_arguments(target, args, kwargs)
    for name, value in list(bound.arguments.items()):
        annotation = hints.get(name)
        if annotation is None or annotation is Any:
            continue
        adapter = TypeAdapter(annotation)
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


__all__ = ["InvocationHandler", "bind_arguments", "coerce_arguments"]
