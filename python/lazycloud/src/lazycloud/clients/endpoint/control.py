from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from shared.http.endpoints import (
    StartEndpointServeRequest,
    StartEndpointServeResponse,
)
from shared.http_transport import HttpChannel

from lazycloud.control import workspace_path


class EndpointControlChannel(Protocol):
    def post(self, path: str, payload: dict[str, Any] | None = None) -> Any: ...


@dataclass
class EndpointControlClient:
    channel: EndpointControlChannel
    workspace: str = "default"
    """Workspace every call acts in, named rather than inferred.

    A user credential reaches every workspace its owner belongs to, so the request
    has to say which one; inside a container the workspace comes from the environment
    the runner pins. Either way the caller states it rather than letting the server
    pick one.
    """

    @classmethod
    def from_endpoint(
        cls,
        endpoint: str,
        *,
        token: str | None = None,
        workspace: str = "default",
        timeout_seconds: float = 10.0,
    ) -> EndpointControlClient:
        return cls(
            channel=HttpChannel(endpoint=endpoint, token=token, timeout_seconds=timeout_seconds),
            workspace=workspace,
        )

    def _scoped(self, path: str) -> str:
        return workspace_path(path, self.workspace)

    def start_serve(self, stub_id: str, *, timeout: int = 0) -> StartEndpointServeResponse:
        request = StartEndpointServeRequest(stub_id=stub_id, timeout=timeout)
        return StartEndpointServeResponse.model_validate(
            self.channel.post(
                self._scoped("/api/v1/endpoints/serve"), request.model_dump(mode="json")
            )
        )


__all__ = [
    "EndpointControlChannel",
    "EndpointControlClient",
]
