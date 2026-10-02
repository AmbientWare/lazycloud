"""Serve an HTTP workload on the listening socket the supervisor passes in.

Endpoints turn a JSON body and the query into arguments and the return value
into a response, as the reference platform did. ASGI and realtime workloads
serve the loaded application as it is. uvicorn speaks HTTP/1.1 and WebSockets
on the socket; the supervisor in front of it admits requests and forwards
them.
"""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import inspect
import json
import os
import socket
import sys
import traceback
from collections.abc import Awaitable, Callable, Iterable, Mapping
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Protocol, TypeGuard, cast
from urllib.parse import parse_qs

import uvicorn
from lazycloud._shared.errors import InvalidInputError
from lazycloud._shared.function_payloads import FunctionPayloadEncoding
from lazycloud._shared.serialization import to_json_value
from pydantic import JsonValue, TypeAdapter

from runner import routed_output
from runner.invocation import call_handler
from runner.protocol_models import (
    HttpKind,
    HttpServing,
    Loaded,
    LoadFailed,
    Output,
    RunnerError,
    Stream,
)

HTTP_FD_ENV = "LAZYCLOUD_HTTP_FD"
# An endpoint reads its whole body before the call, like a task input.
MAX_ENDPOINT_BODY_BYTES = 16 << 20

Scope = dict[str, Any]
Message = dict[str, Any]
Receive = Callable[[], Awaitable[Message]]
Send = Callable[[Message], Awaitable[None]]
ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]

_JSON_VALUE: TypeAdapter[JsonValue] = TypeAdapter(JsonValue)


class _FrameConnection(Protocol):
    def send(self, header: Any, payload: bytes = b"") -> None: ...

    def wait_closed(self) -> None: ...


def serve_http(connection: _FrameConnection, handler: Any, serving: HttpServing) -> int:
    """Serve until the supervisor closes the frame socket."""

    raw_fd = os.environ.pop(HTTP_FD_ENV, "")
    try:
        listener = socket.socket(fileno=int(raw_fd))
    except (ValueError, OSError) as exc:
        connection.send(
            LoadFailed(
                type="load_failed",
                error=RunnerError(
                    type="RunnerStartError", message=f"{HTTP_FD_ENV}={raw_fd!r}: {exc}"
                ),
            )
        )
        return 1
    listener.set_inheritable(False)
    if serving.kind is HttpKind.endpoint:
        app: ASGIApp = EndpointApp(handler, serving.concurrency)
    else:
        app = asgi_application(handler)
    config = uvicorn.Config(
        request_output(app, connection),
        interface="asgi3",
        http="h11",
        ws="wsproto",
        lifespan="auto",
        log_level="warning",
        access_log=False,
        # The supervisor is the only client and keeps its connections.
        timeout_keep_alive=600,
        proxy_headers=True,
        forwarded_allow_ips="*",
    )
    server = uvicorn.Server(config)
    return asyncio.run(_serve(server, listener, connection))


async def _serve(
    server: uvicorn.Server, listener: socket.socket, connection: _FrameConnection
) -> int:
    async def run() -> None:
        # uvicorn exits the process when the application's startup fails.
        with contextlib.suppress(SystemExit):
            await server.serve(sockets=[listener])

    serving = asyncio.ensure_future(run())
    while not server.started and not serving.done():
        await asyncio.sleep(0.005)
    if not server.started:
        await serving
        connection.send(
            LoadFailed(
                type="load_failed",
                error=RunnerError(
                    type="StartupFailed",
                    message="the application's startup failed; its output has the details",
                ),
            )
        )
        return 1
    connection.send(Loaded(type="loaded"))
    closed = asyncio.get_running_loop().run_in_executor(None, connection.wait_closed)
    await asyncio.wait({serving, closed}, return_when=asyncio.FIRST_COMPLETED)
    server.should_exit = True
    await serving
    return 0


REQUEST_ID_HEADER = b"x-request-id"


def request_output(app: ASGIApp, connection: _FrameConnection) -> ASGIApp:
    """Send what each request writes as output frames under its X-Request-Id.

    The edge sets the header on every request, so the platform keeps a
    request's output with its record. Writes outside a request, such as from
    threads the handler starts without its context, go to the container's
    own output.
    """

    def send_output(request_id: str, stream: Stream, data: str) -> None:
        for chunk in routed_output.utf8_chunks(data):
            connection.send(
                Output(type="output", attempt_id="", request_id=request_id, stream=stream), chunk
            )

    async def wrapped(scope: Scope, receive: Receive, send: Send) -> None:
        request_id = ""
        if scope["type"] in ("http", "websocket"):
            for key, value in scope.get("headers", []):
                if bytes(key).lower() == REQUEST_ID_HEADER:
                    request_id = bytes(value).decode("latin-1").strip()
                    break
        if not request_id:
            await app(scope, receive, send)
            return
        with routed_output.attempt_output(request_id, send_output):
            await app(scope, receive, send)

    return wrapped


def asgi_application(handler: Any) -> ASGIApp:
    """The loaded ASGI app. The SDK's wrappers run the user's app through `local`."""

    local = getattr(handler, "local", None)
    target: Callable[..., Any] = local if callable(local) else handler

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        result = target(scope, receive, send)
        if inspect.isawaitable(result):
            await result

    return app


class EndpointApp:
    """An ASGI app calling a function per request, in a pool of `concurrency` threads."""

    def __init__(self, handler: Any, concurrency: int) -> None:
        self.handler = handler
        self.pool = ThreadPoolExecutor(max_workers=concurrency, thread_name_prefix="endpoint")

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "lifespan":
            await _lifespan(receive, send)
            return
        if scope["type"] != "http":
            await send({"type": "websocket.close", "code": 1003})
            return
        status, headers, body = await self._respond(scope, receive)
        await send({"type": "http.response.start", "status": status, "headers": headers})
        await send({"type": "http.response.body", "body": body})

    async def _respond(self, scope: Scope, receive: Receive) -> tuple[int, list[Any], bytes]:
        try:
            body = await _read_body(receive, MAX_ENDPOINT_BODY_BYTES)
            raw_query = scope.get("query_string", b"").decode("latin-1")
            query = parse_qs(raw_query, keep_blank_values=True)
            args, kwargs = http_arguments(body, query)
        except ValueError as exc:
            return error_response(400, str(exc))
        loop = asyncio.get_running_loop()
        # The pool thread runs in the request's context, so its output is
        # the request's.
        context = contextvars.copy_context()
        handler = self.handler

        def run() -> Any:
            return context.run(call_handler, handler, args, kwargs, FunctionPayloadEncoding.Json)

        try:
            result = await loop.run_in_executor(self.pool, run)
            if inspect.isawaitable(result):
                result = await result
            status, headers, body = endpoint_response(result)
        except InvalidInputError as exc:
            return error_response(400, str(exc))
        except Exception as exc:
            print(traceback.format_exc(), file=sys.stderr, end="", flush=True)
            return error_response(500, f"{type(exc).__name__}: {exc}")
        if status < 200:
            return error_response(
                502, "endpoint returned an informational response without a final response"
            )
        if status > 599:
            return error_response(502, f"endpoint returned the invalid status {status}")
        return status, headers, body


async def _lifespan(receive: Receive, send: Send) -> None:
    while True:
        message = await receive()
        if message["type"] == "lifespan.startup":
            await send({"type": "lifespan.startup.complete"})
        elif message["type"] == "lifespan.shutdown":
            await send({"type": "lifespan.shutdown.complete"})
            return


async def _read_body(receive: Receive, limit: int) -> bytes:
    chunks: list[bytes] = []
    size = 0
    while True:
        message = await receive()
        if message["type"] == "http.disconnect":
            raise ValueError("client disconnected")
        chunk = message.get("body", b"")
        size += len(chunk)
        if size > limit:
            raise ValueError(f"request body exceeds {limit} bytes")
        chunks.append(chunk)
        if not message.get("more_body", False):
            return b"".join(chunks)


def http_arguments(
    body: bytes, query: Mapping[str, list[str]]
) -> tuple[tuple[Any, ...], dict[str, Any]]:
    """Arguments from a JSON object body and the query string.

    `args` and `kwargs` keys are used when present; otherwise the whole object
    is the keyword arguments. Query values are added as keyword arguments,
    as floats when they parse as one, and as lists when a key repeats.
    """

    payload = _json_object(body)
    args: list[Any] = []
    kwargs: dict[str, Any] = {}
    if payload:
        payload.pop("result_format", None)
        raw_args = payload.pop("args", None)
        if isinstance(raw_args, list):
            args = cast("list[Any]", raw_args)
        raw_kwargs = payload.pop("kwargs", None)
        if isinstance(raw_kwargs, dict):
            kwargs = dict(cast("dict[str, Any]", raw_kwargs))
        elif payload:
            kwargs = dict(payload)
    for key, values in query.items():
        coerced = [_query_value(value) for value in values]
        kwargs[key] = coerced[0] if len(coerced) == 1 else coerced
    return tuple(args), kwargs


def _json_object(body: bytes) -> dict[str, Any]:
    if not body:
        return {}
    try:
        payload = _JSON_VALUE.validate_json(body)
    except ValueError:
        raise ValueError("invalid request payload") from None
    if not isinstance(payload, dict):
        raise ValueError("request payload must be a JSON object")
    return payload


def _query_value(value: str) -> float | str:
    try:
        return float(value)
    except ValueError:
        return value


Headers = list[tuple[bytes, bytes]]


def endpoint_response(result: Any) -> tuple[int, Headers, bytes]:
    """Map a return value to a response, as the reference platform did.

    Response objects pass through; `(body, status)` and `(body, status,
    headers)` set the status and headers; bytes are octet-stream; strings are
    plain text; anything else, including other tuples, is JSON.
    """

    if _is_response_like(result):
        return _from_response_like(result)
    if _is_tuple(result):
        status = result[1] if len(result) in {2, 3} else None
        if isinstance(status, int) and not isinstance(status, bool):
            _, headers, body = endpoint_response(result[0])
            if len(result) == 3:
                headers = _merge_headers(headers, _headers(result[2]))
            return status, headers, body
        return _json_response(result)
    if isinstance(result, memoryview):
        return 200, [(b"content-type", b"application/octet-stream")], result.tobytes()
    if isinstance(result, bytes | bytearray):
        return 200, [(b"content-type", b"application/octet-stream")], bytes(result)
    if isinstance(result, str):
        return 200, [(b"content-type", b"text/plain; charset=utf-8")], result.encode()
    return _json_response(result)


def error_response(status: int, message: str) -> tuple[int, Headers, bytes]:
    return status, [(b"content-type", b"application/json")], json.dumps({"error": message}).encode()


def _json_response(value: Any) -> tuple[int, Headers, bytes]:
    return 200, [(b"content-type", b"application/json")], json.dumps(to_json_value(value)).encode()


def _is_response_like(value: object) -> bool:
    return all(hasattr(value, name) for name in ("status_code", "headers", "body"))


def _from_response_like(value: Any) -> tuple[int, Headers, bytes]:
    raw: object = value.body
    if isinstance(raw, str):
        body = raw.encode()
    elif isinstance(raw, memoryview):
        body = raw.tobytes()
    elif isinstance(raw, bytes | bytearray):
        body = bytes(raw)
    else:
        body = json.dumps(to_json_value(raw)).encode()
    return int(value.status_code), _headers(value.headers), body


def _merge_headers(base: Headers, extra: Headers) -> Headers:
    names = {name for name, _ in extra}
    return [item for item in base if item[0] not in names] + extra


def _headers(raw: object) -> Headers:
    if _has_multi_items(raw):
        items = raw.multi_items()
    elif _has_items(raw):
        items = raw.items()
    else:
        return []
    headers: Headers = []
    for key, value in items:
        name = str(key).lower().encode("latin-1")
        values: Iterable[object] = (
            cast("Iterable[object]", value) if isinstance(value, list | tuple | set) else [value]
        )
        headers.extend((name, str(item).encode("latin-1")) for item in values)
    return headers


class _HasItems(Protocol):
    def items(self) -> Iterable[tuple[object, object]]: ...


class _HasMultiItems(Protocol):
    def multi_items(self) -> Iterable[tuple[object, object]]: ...


def _has_items(value: object) -> TypeGuard[_HasItems]:
    return callable(getattr(value, "items", None))


def _has_multi_items(value: object) -> TypeGuard[_HasMultiItems]:
    return callable(getattr(value, "multi_items", None))


def _is_tuple(value: object) -> TypeGuard[tuple[object, ...]]:
    return isinstance(value, tuple)


__all__ = [
    "HTTP_FD_ENV",
    "EndpointApp",
    "asgi_application",
    "endpoint_response",
    "error_response",
    "http_arguments",
    "serve_http",
]
