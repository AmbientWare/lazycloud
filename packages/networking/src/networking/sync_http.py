from __future__ import annotations

import http.client
import select
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field

from shared.routing import BackendRouteState

from networking.async_http import (
    IDLE_CONNECTION_SECONDS,
    MAX_IDLE_CONNECTIONS,
    MAX_IDLE_CONNECTIONS_PER_ROUTE,
)
from networking.dialer import BackendRouteConnection, BackendRouteDialer


class BackendHttpConnection(http.client.HTTPConnection):
    def __init__(self, backend: BackendRouteConnection, *, timeout: float) -> None:
        super().__init__("backend.route", timeout=timeout)
        self.backend = backend
        self.sock = backend.socket
        self.sock.settimeout(timeout)
        self.response: http.client.HTTPResponse | None = None
        self.expiration: threading.Timer | None = None
        self.idle_until = 0.0

    def connect(self) -> None:
        raise ConnectionError("Backend HTTP connection is closed")

    def getresponse(self) -> http.client.HTTPResponse:
        self.response = super().getresponse()
        return self.response


@dataclass(slots=True)
class BackendHttpConnectionPool:
    route_dialer: BackendRouteDialer
    _idle: dict[str, list[BackendHttpConnection]] = field(default_factory=dict, init=False)
    _connections: set[BackendHttpConnection] = field(default_factory=set, init=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False)
    _closed: bool = field(default=False, init=False)

    @contextmanager
    def connection(self, route_id: str, timeout: float) -> Iterator[BackendHttpConnection]:
        connection = self._acquire(route_id, timeout)
        reusable = False
        try:
            yield connection
            response = connection.response
            reusable = response is not None and response.isclosed() and not response.will_close
        finally:
            with self._lock:
                if (
                    reusable
                    and not self._closed
                    and self._healthy(connection)
                    and sum(map(len, self._idle.values())) < MAX_IDLE_CONNECTIONS
                    and len(self._idle.get(route_id, ())) < MAX_IDLE_CONNECTIONS_PER_ROUTE
                ):
                    self._idle.setdefault(route_id, []).append(connection)
                    connection.idle_until = time.monotonic() + IDLE_CONNECTION_SECONDS
                    connection.expiration = threading.Timer(
                        IDLE_CONNECTION_SECONDS, self._expire, args=(connection,)
                    )
                    connection.expiration.daemon = True
                    connection.expiration.start()
                else:
                    self._discard(connection)

    def close(self) -> None:
        with self._lock:
            self._closed = True
            for connection in tuple(self._connections):
                self._discard(connection)

    def _acquire(self, route_id: str, timeout: float) -> BackendHttpConnection:
        with self._lock:
            if self._closed:
                raise ConnectionError("Backend HTTP pool is closed")
            idle = self._idle.get(route_id)
            connection = idle.pop() if idle else None
            if idle == []:
                del self._idle[route_id]
            if connection is not None and connection.expiration is not None:
                connection.expiration.cancel()
                connection.expiration = None
        if connection is not None:
            try:
                route = self.route_dialer.resolver.get_backend_route(route_id)
                agent = (
                    self.route_dialer.tunnel.directory.get(route.workspace_id, route.enrollment_id)
                    if route is not None
                    else None
                )
                with self._lock:
                    if (
                        not self._closed
                        and route is not None
                        and route.state is BackendRouteState.Ready
                        and route == connection.backend.route
                        and agent == connection.backend.agent
                        and self._healthy(connection)
                    ):
                        connection.timeout = timeout
                        connection.backend.socket.settimeout(timeout)
                        connection.response = None
                        return connection
            except BaseException:
                with self._lock:
                    self._discard(connection)
                raise
            with self._lock:
                self._discard(connection)
        backend = self.route_dialer.dial_backend_route(route_id, timeout_seconds=timeout)
        connection = BackendHttpConnection(backend, timeout=timeout)
        with self._lock:
            if self._closed:
                connection.close()
                raise ConnectionError("Backend HTTP pool is closed")
            self._connections.add(connection)
        return connection

    @staticmethod
    def _healthy(connection: BackendHttpConnection) -> bool:
        sock = connection.sock
        # An idle HTTP connection cannot have unread data, including a peer EOF.
        if sock is None or sock.fileno() < 0:
            return False
        poller = select.poll()
        poller.register(sock, select.POLLIN | select.POLLERR | select.POLLHUP)
        return not poller.poll(0)

    def _expire(self, connection: BackendHttpConnection) -> None:
        with self._lock:
            if (
                connection in self._idle.get(connection.backend.route.route_id, ())
                and time.monotonic() >= connection.idle_until
            ):
                self._discard(connection)

    def _discard(self, connection: BackendHttpConnection) -> None:
        self._connections.discard(connection)
        route_id = connection.backend.route.route_id
        idle = self._idle.get(route_id)
        if idle is not None and connection in idle:
            idle.remove(connection)
            if not idle:
                del self._idle[route_id]
        if connection.expiration is not None:
            connection.expiration.cancel()
            connection.expiration = None
        connection.close()
