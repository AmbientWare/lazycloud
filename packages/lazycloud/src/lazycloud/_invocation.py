from __future__ import annotations

from collections.abc import Callable
from typing import Any

from shared.callables import bind_arguments, coerce_arguments

from lazycloud.abstractions.metadata import SchemaInput, schema_metadata
from lazycloud.schema import Schema


def prepare_arguments(
    target: Callable[..., Any],
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
    inputs: SchemaInput,
) -> tuple[tuple[Any, ...], dict[str, Any]]:
    if inputs is None:
        return coerce_arguments(target, args, kwargs)
    schema = inputs if isinstance(inputs, Schema) else Schema.from_dict(schema_metadata(inputs))
    bound = bind_arguments(target, args, kwargs)
    bound.apply_defaults()
    bound.arguments.update(schema.validate(bound.arguments))
    return bound.args, bound.kwargs
