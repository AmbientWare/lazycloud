from __future__ import annotations

from collections.abc import AsyncIterable, Mapping

from fastapi import Request, Response, WebSocket
from foundation.http import HOP_BY_HOP_REQUEST_HEADERS
from starlette.responses import StreamingResponse
from websockets.typing import Subprotocol

HOP_BY_HOP_RESPONSE_HEADERS = {
    "connection",
    "content-length",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
}

# Hop-by-hop headers belong to the client-to-control-plane connection only, and
# `proxy-authorization` among them is a credential the backend must never see.
# The handshake headers describe that same first connection: the websockets
# client mints its own for the backend leg, so forwarding the client's would
# also duplicate them.
_BACKEND_WEBSOCKET_HEADER_EXCLUDES = HOP_BY_HOP_REQUEST_HEADERS | {
    "sec-websocket-accept",
    "sec-websocket-extensions",
    "sec-websocket-key",
    "sec-websocket-protocol",
    "sec-websocket-version",
}


def forwarded_path(subpath: str) -> str:
    if not subpath:
        return "/"
    return subpath if subpath.startswith("/") else f"/{subpath}"


def request_query_params(request: Request) -> dict[str, list[str]]:
    values: dict[str, list[str]] = {}
    for key, value in request.query_params.multi_items():
        values.setdefault(key, []).append(value)
    return values


def websocket_query_params(websocket: WebSocket) -> dict[str, list[str]]:
    values: dict[str, list[str]] = {}
    for key, value in websocket.query_params.multi_items():
        values.setdefault(key, []).append(value)
    return values


def request_headers(request: Request) -> dict[str, list[str]]:
    return _raw_headers(request.headers.raw)


def websocket_headers(websocket: WebSocket) -> dict[str, list[str]]:
    return _raw_headers(websocket.headers.raw)


def websocket_subprotocols(websocket: WebSocket) -> list[Subprotocol]:
    values: list[Subprotocol] = []
    for raw_value in websocket.headers.getlist("sec-websocket-protocol"):
        values.extend(Subprotocol(item.strip()) for item in raw_value.split(",") if item.strip())
    return values


def backend_websocket_headers(headers: Mapping[str, list[str]]) -> list[tuple[str, str]]:
    forwarded: list[tuple[str, str]] = []
    for key, values in headers.items():
        if key.lower() in _BACKEND_WEBSOCKET_HEADER_EXCLUDES:
            continue
        forwarded.extend((key, value) for value in values)
    return forwarded


async def close_websocket(websocket: WebSocket, *, code: int, reason: str) -> None:
    try:
        await websocket.close(code=code, reason=reason[:120])
    except RuntimeError:
        return


def forwarded_response(
    *,
    status_code: int,
    headers: Mapping[str, list[str]],
    body: bytes,
) -> Response:
    return _with_forwarded_headers(Response(content=body, status_code=status_code), headers)


def forwarded_streaming_response(
    *,
    status_code: int,
    headers: Mapping[str, list[str]],
    body: AsyncIterable[bytes],
) -> StreamingResponse:
    return _with_forwarded_headers(
        StreamingResponse(content=body, status_code=status_code),
        headers,
    )


def _with_forwarded_headers[ResponseT: Response](
    response: ResponseT,
    headers: Mapping[str, list[str]],
) -> ResponseT:
    for key, values in headers.items():
        if key.lower() in HOP_BY_HOP_RESPONSE_HEADERS:
            continue
        for value in values:
            response.headers.append(key, value)
    return response


def _raw_headers(raw_headers: list[tuple[bytes, bytes]]) -> dict[str, list[str]]:
    headers: dict[str, list[str]] = {}
    for raw_key, raw_value in raw_headers:
        key = raw_key.decode("latin-1")
        value = raw_value.decode("latin-1")
        headers.setdefault(key, []).append(value)
    return headers
