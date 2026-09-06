from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from shared.client_version import RECOMMENDED_CLIENT_VERSION_HEADER
from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send


@dataclass(slots=True)
class ClientVersionMiddleware:
    app: ASGIApp
    recommended_version: Callable[[], str | None]

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_response(message: Message) -> None:
            if message["type"] == "http.response.start":
                version = self.recommended_version()
                if version:
                    MutableHeaders(scope=message)[RECOMMENDED_CLIENT_VERSION_HEADER] = version
            await send(message)

        await self.app(scope, receive, send_response)
