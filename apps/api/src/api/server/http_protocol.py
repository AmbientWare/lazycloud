from __future__ import annotations

import asyncio

import h11
from uvicorn.protocols.http.h11_impl import H11Protocol

HTTP_CONNECTION_MAX_AGE_SECONDS = 60.0


class BoundedHttpProtocol(H11Protocol):
    _connection_lifetime: asyncio.TimerHandle | None = None

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        if not isinstance(transport, asyncio.Transport):
            raise TypeError("The API requires a TCP transport")
        super().connection_made(transport)
        self._connection_lifetime = self.loop.call_later(
            HTTP_CONNECTION_MAX_AGE_SECONDS, self.shutdown
        )

    def connection_lost(self, exc: Exception | None) -> None:
        self._cancel_connection_lifetime()
        super().connection_lost(exc)

    def shutdown(self) -> None:
        self._cancel_connection_lifetime()
        if not self.transport.is_closing():
            # Uvicorn finishes the active response before closing its connection.
            # User streams can therefore outlive the gateway's drain deadline.
            super().shutdown()

    def handle_websocket_upgrade(self, event: h11.Request) -> None:
        self._cancel_connection_lifetime()
        super().handle_websocket_upgrade(event)

    def _cancel_connection_lifetime(self) -> None:
        if self._connection_lifetime is not None:
            self._connection_lifetime.cancel()
            self._connection_lifetime = None
