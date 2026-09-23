from __future__ import annotations

import asyncio
import socket
from collections.abc import AsyncIterable, Mapping
from types import TracebackType
from typing import Self

from fastapi import Request, Response, WebSocket, WebSocketDisconnect, status
from foundation.http import HOP_BY_HOP_REQUEST_HEADERS
from starlette.requests import HTTPConnection
from starlette.responses import StreamingResponse
from websockets.typing import Subprotocol

from api.server.host_routing import invoke_host_workspace_id

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


def request_query_params(request: HTTPConnection) -> dict[str, list[str]]:
    values: dict[str, list[str]] = {}
    for key, value in request.query_params.multi_items():
        # API paths reserve workspace for authorization; workload hosts already name it.
        if key == "workspace" and invoke_host_workspace_id(request.scope) is None:
            continue
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


async def bridge_websocket_to_socket(
    websocket: WebSocket,
    backend: socket.socket,
    buffer_size_bytes: int,
) -> None:
    """Relay bytes both ways until either side closes, then stop the other direction.

    Raises a backend socket failure once both directions have stopped, so the
    caller can close the client with the reason rather than as a normal close.
    """
    async with WebSocketSocketBridge(websocket, early_limit_bytes=0) as bridge:
        await bridge.relay(backend, buffer_size_bytes)


class WebSocketSocketBridge:
    """Relays an accepted websocket to a backend socket that may connect later.

    One task reads the client's messages from construction until the connection ends, so
    the server keeps reading control frames, pongs included, while the backend
    connects. Bytes that arrive before `relay` supplies the backend are held in
    memory and sent first; a client that sends more than `early_limit_bytes`
    meanwhile is closed.
    """

    def __init__(self, websocket: WebSocket, *, early_limit_bytes: int) -> None:
        self._websocket = websocket
        self._early_limit_bytes = early_limit_bytes
        self._early = bytearray()
        self._backend: socket.socket | None = None
        self._reader = asyncio.create_task(self._read_client())

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self._reader.cancel()
        await asyncio.gather(self._reader, return_exceptions=True)

    async def relay(self, backend: socket.socket, buffer_size_bytes: int) -> None:
        """Send the held bytes to `backend`, then relay both ways until either side closes.

        Raises a backend socket failure once both directions have stopped, so the
        caller can close the client with the reason rather than as a normal close.
        """
        reader = self._reader
        backend.setblocking(False)
        loop = asyncio.get_running_loop()
        # The reader keeps holding bytes while each flush is in flight; the
        # backend is handed to it only once nothing is held, so bytes keep order.
        while self._early and not reader.done():
            early = bytes(self._early)
            self._early.clear()
            await loop.sock_sendall(backend, early)
        if reader.done():
            return
        self._backend = backend
        to_client = asyncio.create_task(
            _socket_to_websocket(self._websocket, backend, max(buffer_size_bytes, 1))
        )
        done, pending = await asyncio.wait(
            {reader, to_client},
            return_when=asyncio.FIRST_COMPLETED,
        )
        for task in pending:
            task.cancel()
        results = await asyncio.gather(*done, *pending, return_exceptions=True)
        for result in results:
            if isinstance(result, OSError):
                raise result

    async def _read_client(self) -> None:
        loop = asyncio.get_running_loop()
        while True:
            try:
                message = await self._websocket.receive()
            except WebSocketDisconnect:
                return
            if message["type"] == "websocket.disconnect":
                return
            data: bytes | None = message.get("bytes")
            if data is None:
                text: str | None = message.get("text")
                data = text.encode() if text is not None else b""
            if not data:
                continue
            if self._backend is not None:
                await loop.sock_sendall(self._backend, data)
            elif len(self._early) + len(data) <= self._early_limit_bytes:
                self._early += data
            else:
                await close_websocket(
                    self._websocket,
                    code=status.WS_1009_MESSAGE_TOO_BIG,
                    reason=(
                        f"sent more than {self._early_limit_bytes} bytes "
                        "before the backend connected"
                    ),
                )
                return


async def _socket_to_websocket(
    websocket: WebSocket,
    backend: socket.socket,
    buffer_size_bytes: int,
) -> None:
    loop = asyncio.get_running_loop()
    while True:
        data = await loop.sock_recv(backend, buffer_size_bytes)
        if not data:
            return
        await websocket.send_bytes(data)


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
