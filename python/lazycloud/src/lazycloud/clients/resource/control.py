from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol, TypeVar
from urllib.parse import urlencode

from pydantic import BaseModel, JsonValue
from shared.containers import ContainerStatus
from shared.http.compute import (
    ContainerResponse,
    ContainerWithAppPageResponse,
    MachineListResponse,
    UnitListResponse,
    WorkerListResponse,
)
from shared.http.deployments import (
    DeploymentDetailResponse,
    DevboxResponse,
)
from shared.http.errors import HttpResponseDecodeError
from shared.http_transport import HttpChannel
from shared.urls import url_path_segment

from lazycloud.control import workspace_path, workspace_query

ResponseT = TypeVar("ResponseT", bound=BaseModel)


class ResourceControlChannel(Protocol):
    def get(self, path: str) -> JsonValue: ...

    def post(
        self,
        path: str,
        payload: dict[str, JsonValue] | None = None,
    ) -> JsonValue: ...

    def request(
        self,
        method: str,
        path: str,
        *,
        payload: dict[str, JsonValue] | None = None,
    ) -> JsonValue: ...


@dataclass(slots=True)
class ResourceControlClient:
    """Typed client for durable resources owned by ``/api/v1``."""

    channel: ResourceControlChannel
    workspace: str = "default"

    @classmethod
    def from_endpoint(
        cls,
        endpoint: str,
        *,
        token: str | None = None,
        timeout_seconds: float = 10.0,
        workspace: str = "default",
    ) -> ResourceControlClient:
        return cls(
            channel=HttpChannel(endpoint=endpoint, token=token, timeout_seconds=timeout_seconds),
            workspace=workspace,
        )

    def deployment(self, deployment_id: str) -> DeploymentDetailResponse:
        return _validate_response(
            DeploymentDetailResponse,
            self.channel.get(self._path(f"/api/v1/deployments/{url_path_segment(deployment_id)}")),
        )

    def devbox(self, deployment_id: str) -> DevboxResponse:
        return _validate_response(
            DevboxResponse,
            self.channel.get(
                self._path(f"/api/v1/deployments/{url_path_segment(deployment_id)}/devbox")
            ),
        )

    def list_containers(
        self,
        *,
        stub_ids: Sequence[str] = (),
        statuses: Sequence[ContainerStatus] = (),
        app_id: str | None = None,
        limit: int = 100,
        cursor: str | None = None,
    ) -> ContainerWithAppPageResponse:
        query: list[tuple[str, str | int]] = [
            *workspace_query(self.workspace).items(),
            ("limit", limit),
            *(("stub_id", stub_id) for stub_id in stub_ids),
            *(("status", status.value) for status in statuses),
        ]
        if app_id is not None:
            query.append(("app_id", app_id))
        if cursor is not None:
            query.append(("cursor", cursor))
        return _validate_response(
            ContainerWithAppPageResponse, self.channel.get(f"/api/v1/containers?{urlencode(query)}")
        )

    def stop_container(self, container_id: str) -> ContainerResponse:
        return _validate_response(
            ContainerResponse,
            self.channel.post(
                self._path(f"/api/v1/containers/{url_path_segment(container_id)}/stop")
            ),
        )

    def list_machines(self) -> MachineListResponse:
        return _validate_response(
            MachineListResponse,
            self.channel.get(self._path("/api/v1/machines")),
        )

    def list_units(self) -> UnitListResponse:
        return _validate_response(
            UnitListResponse,
            self.channel.get(self._path("/api/v1/units")),
        )

    def list_workers(self) -> WorkerListResponse:
        return _validate_response(
            WorkerListResponse,
            self.channel.get(self._path("/api/v1/workers")),
        )

    def _path(self, path: str) -> str:
        return workspace_path(path, self.workspace)


def _validate_response(model: type[ResponseT], value: object) -> ResponseT:
    try:
        return model.model_validate(value)
    except ValueError as exc:
        raise HttpResponseDecodeError("resource control returned an invalid response") from exc


__all__ = ["ResourceControlChannel", "ResourceControlClient"]
