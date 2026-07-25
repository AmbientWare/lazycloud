from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol
from urllib.parse import quote

from shared.http.shells import (
    CreateShellInExistingContainerRequest,
    CreateShellInExistingContainerResponse,
    CreateStandaloneShellRequest,
    CreateStandaloneShellResponse,
    ShellConnectPlanResponse,
)
from shared.http_transport import HttpChannel


class ShellControlChannel(Protocol):
    def get(self, path: str) -> Any: ...

    def post(self, path: str, payload: dict[str, Any] | None = None) -> Any: ...


@dataclass
class ShellControlClient:
    channel: ShellControlChannel
    workspace: str = "default"

    @classmethod
    def from_endpoint(
        cls,
        endpoint: str,
        *,
        token: str | None = None,
        timeout_seconds: float = 10.0,
        workspace: str = "default",
    ) -> ShellControlClient:
        return cls(
            channel=HttpChannel(endpoint=endpoint, token=token, timeout_seconds=timeout_seconds),
            workspace=workspace,
        )

    def create_standalone(self, stub_id: str) -> CreateStandaloneShellResponse:
        return self.create_standalone_shell(CreateStandaloneShellRequest(stub_id=stub_id))

    def create_existing(self, container_id: str) -> CreateShellInExistingContainerResponse:
        return self.create_shell_in_existing_container(
            CreateShellInExistingContainerRequest(container_id=container_id)
        )

    def connect_plan(self, stub_id: str, container_id: str) -> ShellConnectPlanResponse:
        path = (
            f"/api/v1/shells/connect-plan/{quote(stub_id, safe='')}/{quote(container_id, safe='')}"
        )
        return ShellConnectPlanResponse.model_validate(self.channel.get(path))

    def create_standalone_shell(
        self,
        request: CreateStandaloneShellRequest,
    ) -> CreateStandaloneShellResponse:
        return CreateStandaloneShellResponse.model_validate(
            self.channel.post(
                self._workspace_path("standalone"),
                request.model_dump(mode="json"),
            )
        )

    def create_shell_in_existing_container(
        self,
        request: CreateShellInExistingContainerRequest,
    ) -> CreateShellInExistingContainerResponse:
        return CreateShellInExistingContainerResponse.model_validate(
            self.channel.post(
                self._workspace_path("existing-container"),
                request.model_dump(mode="json"),
            )
        )

    def _workspace_path(self, suffix: str) -> str:
        return f"/api/v1/shells/{suffix}"


__all__ = [
    "ShellControlChannel",
    "ShellControlClient",
]
