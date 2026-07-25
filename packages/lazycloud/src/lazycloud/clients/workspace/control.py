from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol
from urllib.parse import quote, urlencode

from shared.http.workspaces import (
    WorkspaceAuditListResponse,
    WorkspaceResponse,
    WorkspaceUpdateRequest,
)
from shared.http_transport import HttpChannel


class WorkspaceControlChannel(Protocol):
    def get(self, path: str) -> Any: ...

    def patch(self, path: str, payload: dict[str, Any] | None = None) -> Any: ...


@dataclass(slots=True)
class WorkspaceControlClient:
    channel: WorkspaceControlChannel
    workspace: str = "default"

    @classmethod
    def from_endpoint(
        cls,
        endpoint: str,
        *,
        token: str | None = None,
        timeout_seconds: float = 10.0,
        workspace: str = "default",
    ) -> WorkspaceControlClient:
        return cls(
            channel=HttpChannel(endpoint=endpoint, token=token, timeout_seconds=timeout_seconds),
            workspace=workspace,
        )

    def current(self) -> WorkspaceResponse:
        return WorkspaceResponse.model_validate(self.channel.get(self._path("/current")))

    def rename(self, name: str) -> WorkspaceResponse:
        request = WorkspaceUpdateRequest(name=name)
        return WorkspaceResponse.model_validate(
            self.channel.patch(self._path("/current"), request.model_dump(mode="json"))
        )

    def audit(self, *, limit: int = 50, cursor: str | None = None) -> WorkspaceAuditListResponse:
        query: dict[str, str | int] = {"workspace": self.workspace, "limit": limit}
        if cursor:
            query["cursor"] = cursor
        return WorkspaceAuditListResponse.model_validate(
            self.channel.get(f"/api/v1/workspaces/audit?{urlencode(query)}")
        )

    def _path(self, suffix: str) -> str:
        return f"/api/v1/workspaces{suffix}?workspace={quote(self.workspace, safe='')}"


__all__ = ["WorkspaceControlChannel", "WorkspaceControlClient"]
