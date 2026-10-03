from __future__ import annotations

import inspect
import io
import pickle
from collections.abc import Callable
from typing import Any

import cloudpickle
from lazycloud._shared.callables import InvocationHandler, prepare_callable_arguments
from lazycloud._shared.function_payloads import FunctionPayloadEncoding

from runner.protocol_models import Encoding


def cloudpickle_bytes(value: Any) -> bytes:
    """Cloudpickle through the typed pickle entry point."""

    stream = io.BytesIO()
    pickle.Pickler.dump(cloudpickle.CloudPickler(stream), value)
    return stream.getvalue()


def invoke_handler(
    handler: Callable[..., Any],
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
    *,
    encoding: Encoding,
) -> Any:
    """Call the handler and run a returned awaitable to completion.

    JSON arguments are coerced to the handler's annotations; cloudpickle
    arguments are already Python objects and pass through.
    """

    result = call_handler(handler, args, kwargs, FunctionPayloadEncoding(encoding.value))
    if inspect.isawaitable(result):
        import asyncio

        return asyncio.run(_await(result))
    return result


def call_handler(
    handler: Callable[..., Any],
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
    encoding: FunctionPayloadEncoding,
) -> Any:
    """Call the handler and return what it returned, awaitable or not."""

    if isinstance(handler, InvocationHandler):
        return handler.invoke_arguments(args, kwargs, encoding=encoding)
    args, kwargs = prepare_callable_arguments(handler, args, kwargs, encoding=encoding)
    return handler(*args, **kwargs)


async def _await(value: Any) -> Any:
    return await value


__all__ = ["call_handler", "cloudpickle_bytes", "invoke_handler"]
