from __future__ import annotations

import asyncio
import inspect
import io
import pickle
from collections.abc import Callable
from typing import Any

import cloudpickle
from shared.callables import InvocationHandler, prepare_callable_arguments
from shared.function_payloads import FunctionPayloadEncoding

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

    payload_encoding = FunctionPayloadEncoding(encoding.value)
    if isinstance(handler, InvocationHandler):
        result = handler.invoke_arguments(args, kwargs, encoding=payload_encoding)
    else:
        args, kwargs = prepare_callable_arguments(handler, args, kwargs, encoding=payload_encoding)
        result = handler(*args, **kwargs)
    if inspect.isawaitable(result):
        return asyncio.run(_await(result))
    return result


async def _await(value: Any) -> Any:
    return await value


__all__ = ["cloudpickle_bytes", "invoke_handler"]
