from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol

from pydantic import JsonValue
from shared.http.workspaces import (
    WorkspaceCreateRequest,
    WorkspaceListResponse,
    WorkspaceResponse,
    WorkspaceUpdateRequest,
)
from shared.http_transport import HttpChannel
from shared.urls import url_path_segment

from lazycloud.control import workspace_path


class WorkspaceControlChannel(Protocol):
    def get(self, path: str) -> JsonValue: ...

    def post(
        self,
        path: str,
        payload: Mapping[str, JsonValue] | None = None,
    ) -> JsonValue: ...

    def patch(
        self,
        path: str,
        payload: Mapping[str, JsonValue] | None = None,
    ) -> JsonValue: ...

    def delete(self, path: str) -> JsonValue: ...


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

    def list(self) -> WorkspaceListResponse:
        return WorkspaceListResponse.model_validate(self.channel.get("/api/v1/workspaces"))

    def create(self, name: str) -> WorkspaceResponse:
        request = WorkspaceCreateRequest(name=name)
        return WorkspaceResponse.model_validate(
            self.channel.post("/api/v1/workspaces", request.model_dump(mode="json"))
        )

    def rename(self, name: str) -> WorkspaceResponse:
        request = WorkspaceUpdateRequest(name=name)
        return WorkspaceResponse.model_validate(
            self.channel.patch(self._path("/current"), request.model_dump(mode="json"))
        )

    def delete(self, name: str) -> None:
        self.channel.delete(f"/api/v1/workspaces/{url_path_segment(name)}")

    def _path(self, suffix: str) -> str:
        return workspace_path(f"/api/v1/workspaces{suffix}", self.workspace)


__all__ = ["WorkspaceControlChannel", "WorkspaceControlClient"]
