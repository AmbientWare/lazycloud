from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol
from urllib.parse import quote, urlencode

from shared.http.disks import DiskListResponse, DiskResponse
from shared.http_transport import HttpChannel

from lazycloud.control import workspace_path


class DiskControlChannel(Protocol):
    def get(self, path: str) -> Any: ...

    def delete(self, path: str) -> Any: ...


@dataclass
class DiskControlClient:
    channel: DiskControlChannel
    workspace: str = "default"

    @classmethod
    def from_endpoint(
        cls,
        endpoint: str,
        *,
        token: str | None = None,
        timeout_seconds: float = 10.0,
        workspace: str = "default",
    ) -> DiskControlClient:
        return cls(
            channel=HttpChannel(endpoint=endpoint, token=token, timeout_seconds=timeout_seconds),
            workspace=workspace,
        )

    def list(self, *, cursor: str = "") -> DiskListResponse:
        path = "/api/v1/disks"
        if cursor:
            path = f"{path}?{urlencode({'cursor': cursor})}"
        return DiskListResponse.model_validate(
            self.channel.get(workspace_path(path, self.workspace))
        )

    def get(self, name: str) -> DiskResponse:
        return DiskResponse.model_validate(self.channel.get(self._item_path(name)))

    def delete(self, name: str) -> None:
        self.channel.delete(self._item_path(name))

    def _item_path(self, name: str) -> str:
        return workspace_path(f"/api/v1/disks/{quote(name, safe='')}", self.workspace)


__all__ = ["DiskControlChannel", "DiskControlClient"]
