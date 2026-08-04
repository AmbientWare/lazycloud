from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol, TypeVar
from urllib.parse import urlencode

from pydantic import BaseModel, JsonValue
from shared.containers import ContainerStatus
from shared.http.apps import AppListResponse, AppResponse
from shared.http.compute import (
    ContainerResponse,
    ContainerWithAppPageResponse,
    MachineListResponse,
    PoolCreateRequest,
    PoolListResponse,
    PoolResponse,
    WorkerListResponse,
)
from shared.http.deployments import (
    DeploymentListResponse,
    DeploymentResponse,
    DeploymentScaleRequest,
)
from shared.http.errors import HttpResponseDecodeError
from shared.http.tasks import TaskPageResponse, TaskResponse, TaskStopResponse
from shared.http_transport import HttpChannel
from shared.tasks import TaskStatus
from shared.urls import url_path_segment

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

    def list_tasks(
        self,
        *,
        stub_ids: Sequence[str] = (),
        status: TaskStatus | None = None,
        deployment_id: str | None = None,
        app_id: str | None = None,
        limit: int = 100,
        cursor: str | None = None,
    ) -> TaskPageResponse:
        query: list[tuple[str, str | int]] = [
            ("workspace", self.workspace),
            ("limit", limit),
            *(("stub_id", stub_id) for stub_id in stub_ids),
        ]
        if status is not None:
            query.append(("status", status.value))
        if deployment_id is not None:
            query.append(("deployment_id", deployment_id))
        if app_id is not None:
            query.append(("app_id", app_id))
        if cursor is not None:
            query.append(("cursor", cursor))
        return _validate_response(
            TaskPageResponse, self.channel.get(f"/api/v1/tasks?{urlencode(query)}")
        )

    def list_apps(self, *, active: bool | None = None) -> AppListResponse:
        query: dict[str, str | bool] = {"workspace": self.workspace}
        if active is not None:
            query["active"] = active
        return _validate_response(
            AppListResponse,
            self.channel.get(f"/api/v1/apps?{urlencode(query)}"),
        )

    def app(self, app_id: str) -> AppResponse:
        return _validate_response(
            AppResponse, self.channel.get(self._path(f"/api/v1/apps/{url_path_segment(app_id)}"))
        )

    def pause_app(self, app_id: str) -> AppResponse:
        return _validate_response(
            AppResponse,
            self.channel.post(self._path(f"/api/v1/apps/{url_path_segment(app_id)}/pause")),
        )

    def resume_app(self, app_id: str) -> AppResponse:
        return _validate_response(
            AppResponse,
            self.channel.post(self._path(f"/api/v1/apps/{url_path_segment(app_id)}/resume")),
        )

    def delete_app(self, app_id: str) -> None:
        self.channel.request("DELETE", self._path(f"/api/v1/apps/{url_path_segment(app_id)}"))

    def task(self, task_id: str) -> TaskResponse:
        return _validate_response(
            TaskResponse, self.channel.get(self._path(f"/api/v1/tasks/{url_path_segment(task_id)}"))
        )

    def stop_tasks(self, task_ids: Sequence[str]) -> TaskStopResponse:
        query = [("workspace", self.workspace), *(("task_ids", task_id) for task_id in task_ids)]
        return _validate_response(
            TaskStopResponse, self.channel.request("DELETE", f"/api/v1/tasks?{urlencode(query)}")
        )

    def list_deployments(
        self,
        *,
        active: bool | None = None,
        app_id: str | None = None,
        name: str | None = None,
        latest: bool = False,
        limit: int = 100,
        cursor: str | None = None,
    ) -> DeploymentListResponse:
        query: dict[str, str | int | bool] = {
            "workspace": self.workspace,
            "latest": latest,
            "limit": limit,
        }
        if active is not None:
            query["active"] = active
        if app_id is not None:
            query["app_id"] = app_id
        if name is not None:
            query["name"] = name
        if cursor is not None:
            query["cursor"] = cursor
        return _validate_response(
            DeploymentListResponse, self.channel.get(f"/api/v1/deployments?{urlencode(query)}")
        )

    def deployment(self, deployment_id: str) -> DeploymentResponse:
        return _validate_response(
            DeploymentResponse,
            self.channel.get(self._path(f"/api/v1/deployments/{url_path_segment(deployment_id)}")),
        )

    def stop_deployment(self, deployment_id: str) -> DeploymentResponse:
        return self._deployment_action(deployment_id, "stop")

    def start_deployment(self, deployment_id: str) -> DeploymentResponse:
        return self._deployment_action(deployment_id, "start")

    def scale_deployment(self, deployment_id: str, replicas: int) -> DeploymentResponse:
        request = DeploymentScaleRequest(replicas=replicas)
        return _validate_response(
            DeploymentResponse,
            self.channel.post(
                self._path(f"/api/v1/deployments/{url_path_segment(deployment_id)}/scale"),
                request.model_dump(mode="json"),
            ),
        )

    def delete_deployment(self, deployment_id: str) -> None:
        self.channel.request(
            "DELETE",
            self._path(f"/api/v1/deployments/{url_path_segment(deployment_id)}"),
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
            ("workspace", self.workspace),
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

    def list_pools(self) -> PoolListResponse:
        return _validate_response(
            PoolListResponse,
            self.channel.get(self._path("/api/v1/pools")),
        )

    def create_pool(self, request: PoolCreateRequest) -> PoolResponse:
        return _validate_response(
            PoolResponse,
            self.channel.post(
                self._path("/api/v1/pools"),
                request.model_dump(mode="json"),
            ),
        )

    def delete_pool(self, name: str) -> None:
        self.channel.request("DELETE", self._path(f"/api/v1/pools/{url_path_segment(name)}"))

    def list_workers(self) -> WorkerListResponse:
        return _validate_response(
            WorkerListResponse,
            self.channel.get(self._path("/api/v1/workers")),
        )

    def _deployment_action(self, deployment_id: str, action: str) -> DeploymentResponse:
        return _validate_response(
            DeploymentResponse,
            self.channel.post(
                self._path(
                    f"/api/v1/deployments/{url_path_segment(deployment_id)}/{url_path_segment(action)}"
                )
            ),
        )

    def _path(self, path: str) -> str:
        return f"{path}?{urlencode({'workspace': self.workspace})}"


def _validate_response(model: type[ResponseT], value: object) -> ResponseT:
    try:
        return model.model_validate(value)
    except ValueError as exc:
        raise HttpResponseDecodeError("resource control returned an invalid response") from exc


__all__ = ["ResourceControlChannel", "ResourceControlClient"]
