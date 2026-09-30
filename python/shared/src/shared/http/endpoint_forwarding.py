from __future__ import annotations

import asyncio
import inspect
import json
from collections.abc import Awaitable, Callable, Iterable
from typing import Any, Protocol, TypeAlias, TypedDict, TypeGuard
from urllib.parse import urlencode

from shared.app_identity import NAME
from shared.http.endpoints import EndpointForwardRequest, EndpointForwardResponse
from shared.serialization import to_json_value

HeaderMap: TypeAlias = dict[str, list[str]]
HeaderValue: TypeAlias = str | int | float | bool | bytes | list[str] | tuple[str, ...] | set[str]
StatusTuple: TypeAlias = tuple[object, int] | tuple[object, int, object]


class _RequiredASGIMessage(TypedDict):
    type: str


class ASGIMessage(_RequiredASGIMessage, total=False):
    asgi: dict[str, str]
    http_version: str
    method: str
    scheme: str
    path: str
    raw_path: bytes
    query_string: bytes
    headers: list[tuple[bytes, bytes]]
    client: tuple[str, int]
    server: tuple[str, int]
    status: int
    body: bytes | bytearray | memoryview[int]
    more_body: bool


ASGIReceive: TypeAlias = Callable[[], Awaitable[ASGIMessage]]
ASGISend: TypeAlias = Callable[[ASGIMessage], Awaitable[None]]


class _MultiItemsHeaders(Protocol):
    def multi_items(self) -> Iterable[tuple[str, HeaderValue]]: ...


class _ItemsHeaders(Protocol):
    def items(self) -> Iterable[tuple[str, HeaderValue]]: ...


def resolve_endpoint_result(result: Any) -> Any:
    if inspect.isawaitable(result):
        return asyncio.run(_await_result(result))
    return result


def call_asgi_app(
    app: Callable[[ASGIMessage, ASGIReceive, ASGISend], Awaitable[None] | None],
    request: EndpointForwardRequest,
    *,
    server_name: str = NAME,
) -> EndpointForwardResponse:
    return asyncio.run(_call_asgi_app(app, request, server_name=server_name))


def response_from_endpoint_result(result: Any) -> EndpointForwardResponse:
    if isinstance(result, EndpointForwardResponse):
        return result
    if _is_response_like(result):
        return _response_from_response_like(result)
    if _is_object_tuple(result):
        if _is_status_tuple(result):
            body, status_code, headers = _tuple_response_parts(result)
            response = response_from_endpoint_result(body)
            response.status_code = status_code
            response.headers.update(_headers_from_mapping(headers))
            return response
        return EndpointForwardResponse(
            body=json.dumps(to_json_value(result)).encode("utf-8"),
            headers={"content-type": ["application/json"]},
        )
    if isinstance(result, bytes | bytearray):
        return EndpointForwardResponse(
            body=bytes(result),
            headers={"content-type": ["application/octet-stream"]},
        )
    if isinstance(result, memoryview):
        return EndpointForwardResponse(
            body=result.tobytes(),
            headers={"content-type": ["application/octet-stream"]},
        )
    if isinstance(result, str):
        return EndpointForwardResponse(
            body=result.encode("utf-8"),
            headers={"content-type": ["text/plain; charset=utf-8"]},
        )
    return EndpointForwardResponse(
        body=json.dumps(to_json_value(result)).encode("utf-8"),
        headers={"content-type": ["application/json"]},
    )


def error_response(status_code: int, message: str) -> EndpointForwardResponse:
    return EndpointForwardResponse(
        status_code=status_code,
        headers={"content-type": ["application/json"]},
        body=json.dumps({"error": message}).encode("utf-8"),
    )


async def _await_result(result: Awaitable[Any]) -> Any:
    return await result


async def _call_asgi_app(
    app: Callable[[ASGIMessage, ASGIReceive, ASGISend], Awaitable[None] | None],
    request: EndpointForwardRequest,
    *,
    server_name: str,
) -> EndpointForwardResponse:
    body_sent = False
    status_code = 500
    response_headers: HeaderMap = {}
    body_parts: list[bytes] = []

    async def receive() -> ASGIMessage:
        nonlocal body_sent
        if body_sent:
            return {"type": "http.request", "body": b"", "more_body": False}
        body_sent = True
        return {"type": "http.request", "body": request.body, "more_body": False}

    async def send(message: ASGIMessage) -> None:
        nonlocal status_code, response_headers
        message_type = str(message.get("type", ""))
        if message_type == "http.response.start":
            status_code = int(message.get("status", 200))
            response_headers = _headers_from_asgi(message.get("headers", []))
            return
        if message_type == "http.response.body":
            body = message.get("body", b"")
            if isinstance(body, bytes | bytearray | memoryview):
                body_parts.append(_bytes_body(body))

    result = app(_asgi_scope(request, server_name=server_name), receive, send)
    if inspect.isawaitable(result):
        await result
    return EndpointForwardResponse(
        status_code=status_code,
        headers=response_headers,
        body=b"".join(body_parts),
    )


def _asgi_scope(request: EndpointForwardRequest, *, server_name: str) -> ASGIMessage:
    return {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": request.method,
        "scheme": "http",
        "path": request.path,
        "raw_path": request.path.encode("utf-8"),
        "query_string": _query_string(request.query_params),
        "headers": _asgi_request_headers(request.headers),
        "client": ("127.0.0.1", 0),
        "server": (server_name, 80),
    }


def _query_string(query_params: dict[str, list[str]]) -> bytes:
    values = [(key, value) for key, query_values in query_params.items() for value in query_values]
    return urlencode(values).encode("utf-8")


def _asgi_request_headers(headers: HeaderMap) -> list[tuple[bytes, bytes]]:
    return [
        (key.lower().encode("latin-1"), value.encode("latin-1"))
        for key, values in headers.items()
        for value in values
    ]


def _headers_from_asgi(raw_headers: list[tuple[bytes, bytes]]) -> HeaderMap:
    headers: HeaderMap = {}
    for raw_key, raw_value in raw_headers:
        key = _decode_header(raw_key).lower()
        value = _decode_header(raw_value)
        headers.setdefault(key, []).append(value)
    return headers


def _decode_header(value: bytes | bytearray | memoryview[int]) -> str:
    return _bytes_body(value).decode("latin-1")


def _bytes_body(value: bytes | bytearray | memoryview[int]) -> bytes:
    if isinstance(value, bytes):
        return value
    if isinstance(value, bytearray):
        return bytes(value)
    return value.tobytes()


def _is_response_like(result: object) -> bool:
    return hasattr(result, "status_code") and hasattr(result, "headers") and hasattr(result, "body")


def _response_from_response_like(result: object) -> EndpointForwardResponse:
    body = getattr(result, "body", b"")
    if isinstance(body, str):
        body = body.encode("utf-8")
    if isinstance(body, memoryview):
        body = body.tobytes()
    elif isinstance(body, bytearray):
        body = bytes(body)
    elif not isinstance(body, bytes):
        body = json.dumps(to_json_value(body)).encode("utf-8")
    status_code = int(getattr(result, "status_code", 200))
    headers = _headers_from_mapping(getattr(result, "headers", {}))
    return EndpointForwardResponse(
        status_code=status_code,
        headers=headers,
        body=body,
    )


def _is_object_tuple(value: object) -> TypeGuard[tuple[object, ...]]:
    return isinstance(value, tuple)


def _is_status_tuple(result: tuple[object, ...]) -> TypeGuard[StatusTuple]:
    return len(result) in {2, 3} and isinstance(result[1], int)


def _tuple_response_parts(result: StatusTuple) -> tuple[object, int, _ItemsHeaders]:
    headers: _ItemsHeaders = {}
    if len(result) == 3 and _has_items(result[2]):
        headers = result[2]
    return result[0], result[1], headers


def _headers_from_mapping(raw_headers: object) -> HeaderMap:
    headers: HeaderMap = {}
    if _has_multi_items(raw_headers):
        items = raw_headers.multi_items()
    elif _has_items(raw_headers):
        items = raw_headers.items()
    else:
        return headers
    for raw_key, raw_value in items:
        key = str(raw_key)
        if isinstance(raw_value, list | tuple | set):
            headers.setdefault(key, []).extend(str(value) for value in raw_value)
            continue
        headers.setdefault(key, []).append(str(raw_value))
    return headers


def _has_multi_items(value: object) -> TypeGuard[_MultiItemsHeaders]:
    return callable(getattr(value, "multi_items", None))


def _has_items(value: object) -> TypeGuard[_ItemsHeaders]:
    return callable(getattr(value, "items", None))


__all__ = [
    "ASGIMessage",
    "ASGIReceive",
    "ASGISend",
    "HeaderMap",
    "call_asgi_app",
    "error_response",
    "resolve_endpoint_result",
    "response_from_endpoint_result",
]
