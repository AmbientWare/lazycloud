from __future__ import annotations

from collections.abc import Awaitable, Callable

from shared.http import endpoint_forwarding
from shared.http.endpoint_forwarding import (
    ASGIMessage,
    ASGIReceive,
    ASGISend,
    error_response,
    resolve_endpoint_result,
    response_from_endpoint_result,
)
from shared.http.endpoints import EndpointForwardRequest, EndpointForwardResponse


def call_asgi_app(
    app: Callable[[ASGIMessage, ASGIReceive, ASGISend], Awaitable[None] | None],
    request: EndpointForwardRequest,
) -> EndpointForwardResponse:
    return endpoint_forwarding.call_asgi_app(app, request, server_name="runner")


__all__ = [
    "call_asgi_app",
    "error_response",
    "resolve_endpoint_result",
    "response_from_endpoint_result",
]
