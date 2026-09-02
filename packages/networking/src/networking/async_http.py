from __future__ import annotations

import asyncio
import socket
import ssl
from collections.abc import AsyncIterable, AsyncIterator, Mapping
from contextlib import suppress
from dataclasses import dataclass, field

import h11
from foundation.http import grouped_response_headers
from shared.urls import parse_http_address

from networking.dialer import (
    BackendRouteDialer,
    BackendRouteDialerConfig,
    BackendRouteResolver,
)
from networking.routing import build_backend_route_dial_plan


class AsyncBackendHttpError(ConnectionError):
    pass


class AsyncBackendConnectError(AsyncBackendHttpError):
    pass


class AsyncBackendResponseError(AsyncBackendHttpError):
    pass


@dataclass(slots=True)
class AsyncBackendHttpResponse:
    status_code: int
    headers: dict[str, list[str]]
    _reader: asyncio.StreamReader
    _writer: asyncio.StreamWriter
    _connection: h11.Connection
    _timeout_seconds: float

    async def iter_chunks(self) -> AsyncIterator[bytes]:
        try:
            try:
                while True:
                    event = self._connection.next_event()
                    if event is h11.NEED_DATA:
                        data = await asyncio.wait_for(
                            self._reader.read(64 * 1024),
                            timeout=self._timeout_seconds,
                        )
                        self._connection.receive_data(data)
                        continue
                    if isinstance(event, h11.Data):
                        yield bytes(event.data)
                        continue
                    if isinstance(event, (h11.EndOfMessage, h11.ConnectionClosed)):
                        return
                    if event is h11.PAUSED:
                        return
            except (ConnectionError, OSError, TimeoutError, h11.ProtocolError) as exc:
                raise AsyncBackendResponseError(str(exc)) from exc
        finally:
            await self.close()

    async def read(self) -> bytes:
        return b"".join([chunk async for chunk in self.iter_chunks()])

    async def close(self) -> None:
        if self._writer.is_closing():
            return
        self._writer.close()
        try:
            await self._writer.wait_closed()
        except (ConnectionError, OSError):
            return


@dataclass(slots=True)
class AsyncBackendHttpClient:
    route_resolver: BackendRouteResolver | None = None
    route_dialer_config: BackendRouteDialerConfig = field(default_factory=BackendRouteDialerConfig)
    tls_context: ssl.SSLContext | None = None
    _tls_context_lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False, repr=False)

    async def open_stream(
        self,
        *,
        address: str,
        route_id: str,
        method: str,
        path: str,
        headers: Mapping[str, str],
        body: bytes | AsyncIterable[bytes],
        timeout_seconds: float,
        connect_timeout_seconds: float | None = None,
        content_length: int | None = None,
        resource: str,
    ) -> AsyncBackendHttpResponse:
        timeout = timeout_seconds or self.route_dialer_config.timeout_seconds
        connect_timeout = connect_timeout_seconds or timeout
        body_length = len(body) if isinstance(body, bytes) else content_length
        if body_length is None or body_length < 0:
            raise ValueError("streamed HTTP requests require a nonnegative content length")
        reader, writer, host = await self._open_connection(
            address=address,
            route_id=route_id,
            timeout_seconds=connect_timeout,
            resource=resource,
        )

        connection = h11.Connection(h11.CLIENT)
        request_headers = [
            (name.encode("latin-1"), value.encode("latin-1"))
            for name, value in headers.items()
            if name.lower() not in {"content-length", "host"}
        ]
        request_headers.extend(
            [
                (b"host", host.encode("latin-1")),
                (b"content-length", str(body_length).encode("ascii")),
                (b"connection", b"close"),
            ]
        )
        try:
            writer.write(
                connection.send(
                    h11.Request(
                        method=method,
                        target=path,
                        headers=request_headers,
                    )
                )
            )
            if isinstance(body, bytes):
                if body:
                    writer.write(connection.send(h11.Data(data=body)))
            else:
                async for chunk in body:
                    if not chunk:
                        continue
                    writer.write(connection.send(h11.Data(data=chunk)))
                    await asyncio.wait_for(writer.drain(), timeout=timeout)
            writer.write(connection.send(h11.EndOfMessage()))
            await asyncio.wait_for(writer.drain(), timeout=timeout)
            response = await self._response_head(connection, reader, timeout)
            return AsyncBackendHttpResponse(
                status_code=response.status_code,
                headers=grouped_response_headers(
                    (name.decode("latin-1"), value.decode("latin-1"))
                    for name, value in response.headers
                ),
                _reader=reader,
                _writer=writer,
                _connection=connection,
                _timeout_seconds=timeout,
            )
        except BaseException as exc:
            writer.close()
            with suppress(ConnectionError, OSError):
                await writer.wait_closed()
            if isinstance(
                exc,
                (ConnectionError, OSError, TimeoutError, UnicodeError, h11.ProtocolError),
            ):
                raise AsyncBackendResponseError(str(exc)) from exc
            raise

    async def probe_connection(
        self,
        *,
        address: str,
        route_id: str,
        timeout_seconds: float,
        resource: str,
    ) -> None:
        _reader, writer, _host = await self._open_connection(
            address=address,
            route_id=route_id,
            timeout_seconds=timeout_seconds,
            resource=resource,
        )
        writer.close()
        with suppress(ConnectionError, OSError):
            await writer.wait_closed()

    async def open_route_socket(self, route_id: str, timeout_seconds: float) -> socket.socket:
        """A connected, non-blocking socket to the backend route, dialed off the loop."""

        backend_socket = await asyncio.to_thread(self._route_socket, route_id, timeout_seconds)
        backend_socket.setblocking(False)
        return backend_socket

    def _route_socket(self, route_id: str, timeout_seconds: float) -> socket.socket:
        config = self.route_dialer_config.model_copy(
            update={
                "timeout_seconds": min(
                    timeout_seconds,
                    self.route_dialer_config.timeout_seconds,
                )
            }
        )
        connection = BackendRouteDialer(
            resolver=self.route_resolver,
            config=config,
        ).dial_plan(build_backend_route_dial_plan(route_id))
        if not isinstance(connection, socket.socket):
            connection.close()
            raise TypeError("backend route dialer returned a non-socket connection")
        return connection

    async def _open_connection(
        self,
        *,
        address: str,
        route_id: str,
        timeout_seconds: float,
        resource: str,
    ) -> tuple[asyncio.StreamReader, asyncio.StreamWriter, str]:
        backend_socket: socket.socket | None = None
        try:
            if route_id:
                backend_socket = await self.open_route_socket(route_id, timeout_seconds)
                reader, writer = await asyncio.wait_for(
                    asyncio.open_connection(sock=backend_socket),
                    timeout=timeout_seconds,
                )
                return reader, writer, "backend.route"
            parsed = parse_http_address(address, resource=resource)
            tls = await self._https_context() if parsed.scheme == "https" else None
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(
                    parsed.hostname or "",
                    parsed.port or (443 if tls is not None else 80),
                    ssl=tls,
                    server_hostname=parsed.hostname if tls is not None else None,
                ),
                timeout=timeout_seconds,
            )
            return reader, writer, parsed.netloc
        except (OSError, RuntimeError, TimeoutError) as exc:
            if backend_socket is not None:
                backend_socket.close()
            raise AsyncBackendConnectError(str(exc)) from exc

    async def _https_context(self) -> ssl.SSLContext:
        if self.tls_context is not None:
            return self.tls_context
        async with self._tls_context_lock:
            if self.tls_context is None:
                self.tls_context = await asyncio.to_thread(ssl.create_default_context)
            return self.tls_context

    @staticmethod
    async def _response_head(
        connection: h11.Connection,
        reader: asyncio.StreamReader,
        timeout_seconds: float,
    ) -> h11.Response:
        while True:
            event = connection.next_event()
            if event is h11.NEED_DATA:
                data = await asyncio.wait_for(
                    reader.read(64 * 1024),
                    timeout=timeout_seconds,
                )
                connection.receive_data(data)
                continue
            if isinstance(event, h11.InformationalResponse):
                continue
            if isinstance(event, h11.Response):
                return event
            raise ConnectionError("backend closed before sending an HTTP response")


__all__ = [
    "AsyncBackendConnectError",
    "AsyncBackendHttpClient",
    "AsyncBackendHttpError",
    "AsyncBackendHttpResponse",
    "AsyncBackendResponseError",
]
