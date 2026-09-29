from __future__ import annotations

import asyncio
import hashlib
import socket
from collections.abc import AsyncIterable, AsyncIterator, Mapping
from contextlib import suppress
from dataclasses import dataclass, field

import h11
from foundation.http import grouped_response_headers
from shared.agent_connections import AgentConnectionRecord
from shared.routing import AgentBackendRoute, BackendRouteState

from networking.dialer import BackendRouteConnection, BackendRouteDialer

MAX_IDLE_CONNECTIONS = 16
MAX_IDLE_CONNECTIONS_PER_ROUTE = 2
IDLE_CONNECTION_SECONDS = 2.0


class AsyncBackendHttpError(ConnectionError):
    pass


class AsyncBackendConnectError(AsyncBackendHttpError):
    pass


class AsyncBackendResponseError(AsyncBackendHttpError):
    pass


class AsyncBackendTimeoutError(AsyncBackendResponseError):
    pass


@dataclass(slots=True, eq=False)
class _HttpConnection:
    route: AgentBackendRoute
    agent: AgentConnectionRecord
    reader: asyncio.StreamReader
    writer: asyncio.StreamWriter
    protocol: h11.Connection = field(default_factory=lambda: h11.Connection(h11.CLIENT))
    expiration: asyncio.TimerHandle | None = None


@dataclass(slots=True)
class AsyncBackendHttpResponse:
    status_code: int
    headers: dict[str, list[str]]
    _client: AsyncBackendHttpClient
    _connection: _HttpConnection
    _timeout_seconds: float
    _complete: bool = False
    _closed: bool = False

    async def iter_chunks(self) -> AsyncIterator[bytes]:
        if self._closed:
            raise AsyncBackendResponseError("backend response is closed")
        try:
            try:
                while True:
                    event = self._connection.protocol.next_event()
                    if event is h11.NEED_DATA:
                        data = await asyncio.wait_for(
                            self._connection.reader.read(64 * 1024),
                            timeout=self._timeout_seconds,
                        )
                        self._connection.protocol.receive_data(data)
                        continue
                    if isinstance(event, h11.Data):
                        yield bytes(event.data)
                        continue
                    if isinstance(event, h11.EndOfMessage):
                        self._complete = True
                        return
                    if isinstance(event, h11.ConnectionClosed):
                        return
                    if event is h11.PAUSED:
                        return
            except TimeoutError as exc:
                raise AsyncBackendTimeoutError("backend response timed out") from exc
            except (ConnectionError, OSError, h11.ProtocolError) as exc:
                raise AsyncBackendResponseError(str(exc)) from exc
        finally:
            await self.close()

    async def read(self) -> bytes:
        return b"".join([chunk async for chunk in self.iter_chunks()])

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        await self._client._release(self._connection, reusable=self._complete)


@dataclass(slots=True)
class AsyncBackendHttpClient:
    route_dialer: BackendRouteDialer
    _connections: set[_HttpConnection] = field(default_factory=set, init=False)
    _idle: dict[str, list[_HttpConnection]] = field(default_factory=dict, init=False)
    _closed: bool = field(default=False, init=False)

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
        timeout = timeout_seconds or self.route_dialer.config.timeout_seconds
        connect_timeout = connect_timeout_seconds or timeout
        body_length = len(body) if isinstance(body, bytes) else content_length
        if body_length is None or body_length < 0:
            raise ValueError("streamed HTTP requests require a nonnegative content length")
        request_headers = [
            (name.encode("latin-1"), value.encode("latin-1"))
            for name, value in headers.items()
            if name.lower() not in {"content-length", "host", "connection"}
        ]
        request_headers.extend(
            [
                (b"host", b"backend.route"),
                (b"content-length", str(body_length).encode("ascii")),
            ]
        )
        backend = await self._acquire(
            address=address,
            route_id=route_id,
            timeout_seconds=connect_timeout,
            resource=resource,
        )
        reader, writer, connection = backend.reader, backend.writer, backend.protocol
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
                _client=self,
                _connection=backend,
                _timeout_seconds=timeout,
            )
        except BaseException as exc:
            await self._release(backend, reusable=False)
            if isinstance(exc, TimeoutError):
                raise AsyncBackendTimeoutError("backend response timed out") from exc
            if isinstance(
                exc,
                (ConnectionError, OSError, UnicodeError, h11.ProtocolError),
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
        connection = await self._open_connection(
            address=address,
            route_id=route_id,
            timeout_seconds=timeout_seconds,
            resource=resource,
        )
        await self._release(connection, reusable=False)

    async def close(self) -> None:
        self._closed = True
        connections = tuple(self._connections)
        for connection in connections:
            self._discard(connection)
        await asyncio.gather(
            *(connection.writer.wait_closed() for connection in connections),
            return_exceptions=True,
        )

    async def readiness_revision(self, route_id: str, address: str) -> str | None:
        if not route_id:
            return hashlib.sha256(address.encode()).hexdigest()
        route = await asyncio.to_thread(self.route_dialer.resolver.get_backend_route, route_id)
        if route is None or route.state is not BackendRouteState.Ready:
            return None
        agent = await asyncio.to_thread(
            self.route_dialer.tunnel.directory.get, route.workspace_id, route.enrollment_id
        )
        if agent is None:
            return None
        return hashlib.sha256(
            f"{route.model_dump_json()}:{agent.connection_id}:{address}".encode()
        ).hexdigest()

    async def _acquire(
        self,
        *,
        address: str,
        route_id: str,
        timeout_seconds: float,
        resource: str,
    ) -> _HttpConnection:
        if self._closed:
            raise AsyncBackendConnectError("backend HTTP client is closed")
        if self._idle.get(route_id):
            # A retained stream must not carry new requests after its route or agent changes.
            try:
                route = await asyncio.to_thread(
                    self.route_dialer.resolver.get_backend_route, route_id
                )
                agent = (
                    await asyncio.to_thread(
                        self.route_dialer.tunnel.directory.get,
                        route.workspace_id,
                        route.enrollment_id,
                    )
                    if route is not None
                    else None
                )
            except Exception:
                for connection in tuple(self._idle.get(route_id, ())):
                    self._discard(connection)
                raise
            while idle := self._idle.get(route_id):
                connection = idle.pop()
                if not idle:
                    del self._idle[route_id]
                if connection.expiration is not None:
                    connection.expiration.cancel()
                    connection.expiration = None
                if (
                    route is not None
                    and route.state is BackendRouteState.Ready
                    and route == connection.route
                    and agent == connection.agent
                    and not connection.reader.at_eof()
                    and not connection.writer.is_closing()
                ):
                    return connection
                self._discard(connection)
        return await self._open_connection(
            address=address,
            route_id=route_id,
            timeout_seconds=timeout_seconds,
            resource=resource,
        )

    async def _release(self, connection: _HttpConnection, *, reusable: bool) -> None:
        if (
            reusable
            and not self._closed
            and not connection.reader.at_eof()
            and not connection.writer.is_closing()
            and connection.protocol.our_state is h11.DONE
            and connection.protocol.their_state is h11.DONE
            and not connection.protocol.trailing_data[0]
            and sum(map(len, self._idle.values())) < MAX_IDLE_CONNECTIONS
            and len(self._idle.get(connection.route.route_id, ())) < MAX_IDLE_CONNECTIONS_PER_ROUTE
        ):
            connection.protocol.start_next_cycle()
            self._idle.setdefault(connection.route.route_id, []).append(connection)
            connection.expiration = asyncio.get_running_loop().call_later(
                IDLE_CONNECTION_SECONDS, self._discard, connection
            )
            return
        self._discard(connection)
        with suppress(ConnectionError, OSError):
            await connection.writer.wait_closed()

    def _discard(self, connection: _HttpConnection) -> None:
        self._connections.discard(connection)
        idle = self._idle.get(connection.route.route_id)
        if idle is not None and connection in idle:
            idle.remove(connection)
            if not idle:
                del self._idle[connection.route.route_id]
        if connection.expiration is not None:
            connection.expiration.cancel()
            connection.expiration = None
        connection.writer.close()

    async def open_route_socket(self, route_id: str, timeout_seconds: float) -> socket.socket:
        """A connected, non-blocking socket to the backend route, dialed off the loop."""

        backend_socket = (await self._route_connection(route_id, timeout_seconds)).socket
        backend_socket.setblocking(False)
        return backend_socket

    async def _route_connection(
        self, route_id: str, timeout_seconds: float
    ) -> BackendRouteConnection:
        pending = asyncio.create_task(
            asyncio.to_thread(
                self.route_dialer.dial_backend_route, route_id, timeout_seconds=timeout_seconds
            )
        )
        try:
            return await asyncio.shield(pending)
        except asyncio.CancelledError:

            def close_cancelled(task: asyncio.Task[BackendRouteConnection]) -> None:
                if not task.cancelled() and task.exception() is None:
                    task.result().socket.close()

            pending.add_done_callback(close_cancelled)
            raise

    async def _open_connection(
        self,
        *,
        address: str,
        route_id: str,
        timeout_seconds: float,
        resource: str,
    ) -> _HttpConnection:
        backend_socket: socket.socket | None = None
        try:
            if not route_id:
                raise AsyncBackendConnectError("Backend request must name an authorized route")
            backend = await self._route_connection(route_id, timeout_seconds)
            backend_socket = backend.socket
            backend_socket.setblocking(False)
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(sock=backend_socket),
                timeout=timeout_seconds,
            )
            connection = _HttpConnection(backend.route, backend.agent, reader, writer)
            if self._closed:
                writer.close()
                raise AsyncBackendConnectError("backend HTTP client is closed")
            self._connections.add(connection)
            return connection
        except BaseException as exc:
            if backend_socket is not None:
                backend_socket.close()
            if isinstance(exc, (OSError, RuntimeError, TimeoutError)):
                raise AsyncBackendConnectError(str(exc)) from exc
            raise

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
    "AsyncBackendTimeoutError",
]
