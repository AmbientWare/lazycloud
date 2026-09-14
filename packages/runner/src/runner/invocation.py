from __future__ import annotations

import asyncio
import inspect
import io
import pickle
from collections.abc import Callable
from typing import Any

import cloudpickle
from shared.callables import InvocationHandler, coerce_arguments


def cloudpickle_bytes(value: Any) -> bytes:
    stream = io.BytesIO()
    pickler = cloudpickle.CloudPickler(stream)
    pickle.Pickler.dump(pickler, value)
    return stream.getvalue()


def invoke_handler(
    handler: Callable[..., Any],
    /,
    *args: Any,
    **kwargs: Any,
) -> Any:
    if isinstance(handler, InvocationHandler):
        result = handler.invoke_arguments(args, kwargs)
    else:
        args, kwargs = coerce_arguments(handler, args, kwargs)
        result = handler(*args, **kwargs)
    if inspect.isawaitable(result):
        return asyncio.run(_await_any(result))
    return result


async def _await_any(value: Any) -> Any:
    return await value


__all__ = ["cloudpickle_bytes", "invoke_handler"]
