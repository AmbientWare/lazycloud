from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from shared.http.endpoints import (
    StartEndpointServeRequest,
    StartEndpointServeResponse,
)
from shared.http_transport import HttpChannel


class EndpointControlChannel(Protocol):
    def post(self, path: str, payload: dict[str, Any] | None = None) -> Any: ...


@dataclass
class EndpointControlClient:
    channel: EndpointControlChannel

    @classmethod
    def from_endpoint(
        cls,
        endpoint: str,
        *,
        token: str | None = None,
        timeout_seconds: float = 10.0,
    ) -> EndpointControlClient:
        return cls(
            channel=HttpChannel(endpoint=endpoint, token=token, timeout_seconds=timeout_seconds)
        )

    def start_serve(self, stub_id: str, *, timeout: int = 0) -> StartEndpointServeResponse:
        request = StartEndpointServeRequest(stub_id=stub_id, timeout=timeout)
        return StartEndpointServeResponse.model_validate(
            self.channel.post("/api/v1/endpoints/serve", request.model_dump(mode="json"))
        )


__all__ = [
    "EndpointControlChannel",
    "EndpointControlClient",
]
