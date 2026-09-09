from __future__ import annotations

from collections.abc import Callable
from ipaddress import ip_address

from anyio import CancelScope
from observability.network_transfers import OutboundTransferMeter, TransferAttribution
from observability.usage import UsageService
from starlette.requests import HTTPConnection
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from api.server.client_address import client_address


def attribute_public_transfer(
    connection: HTTPConnection,
    *,
    workspace_id: str,
    resource_type: str,
    resource_id: str,
    stub_id: str = "",
) -> None:
    """Called after authorization resolves the resource that owns the response."""
    connection.state.public_transfer = TransferAttribution(
        workspace_id=workspace_id,
        resource_type=resource_type,
        resource_id=resource_id,
        stub_id=stub_id,
    )


class PublicTransferMiddleware:
    def __init__(
        self,
        app: ASGIApp,
        *,
        usage: Callable[[], UsageService],
        client_ip_header: str,
    ) -> None:
        self.app = app
        self.usage = usage
        self.client_ip_header = client_ip_header

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in {"http", "websocket"}:
            await self.app(scope, receive, send)
            return
        # Host routing copies the scope; the state mapping must stay shared.
        state = scope.setdefault("state", {})
        destination = client_address(scope, header_name=self.client_ip_header)
        billable = bool(destination) and ip_address(destination).is_global
        meter: OutboundTransferMeter | None = None

        async def measured_send(message: Message) -> None:
            nonlocal meter
            await send(message)
            byte_count = 0
            if message["type"] == "http.response.body":
                byte_count = len(message.get("body", b""))
            elif message["type"] == "websocket.send":
                byte_count = len(message.get("bytes") or b"") + len(
                    (message.get("text") or "").encode("utf-8")
                )
            attribution = state.get("public_transfer")
            if not byte_count or not isinstance(attribution, TransferAttribution):
                return
            if meter is None:
                meter = OutboundTransferMeter(
                    usage=self.usage(),
                    attribution=attribution,
                    transport=scope["type"],
                    billable=billable,
                )
            await meter.sent(byte_count)

        try:
            await self.app(scope, receive, measured_send)
        finally:
            if meter is not None:
                with CancelScope(shield=True):
                    await meter.flush()
