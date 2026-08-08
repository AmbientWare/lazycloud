from __future__ import annotations

import hashlib
import mimetypes
from dataclasses import dataclass, field
from pathlib import Path

from shared.events import Event
from shared.http.deployments import DeploymentResponse
from shared.http.observability import EventHistoryRequest, EventQueryResponse
from shared.http.tasks import TaskResponse
from shared.http_transport import HttpChannel
from shared.tasks import TaskStatus
from typing_extensions import Self

from lazycloud.clients.compute.control import ComputeClient, ComputeControlChannel
from lazycloud.clients.observability.control import ObservabilityClient
from lazycloud.config import ClientProfile, get_profile
from lazycloud.control import ControlClientConfig, resolve_control_client_config
from lazycloud.control_clients import control_http_channel, observability_control_client
from lazycloud.session.deployment import (
    Deployment,
    DeploymentClient,
    DeploymentControlClient,
    DeploymentResourceClient,
    DeploymentSubmission,
)
from lazycloud.session.task import (
    FunctionCall,
    Task,
    TaskBatch,
    TaskClient,
    TaskControlClient,
    TaskSubscription,
)
from lazycloud.session.uploads import (
    object_upload_timeout_seconds,
    stream_object_bytes,
    stream_object_file,
)
from lazycloud.terminal import ProgressCallback


@dataclass(frozen=True, slots=True)
class UploadedObject:
    object_id: str
    name: str
    bucket: str
    size: int
    sha256: str


@dataclass(slots=True)
class Client:
    profile: ClientProfile | None = None
    workspace: str | None = None
    deployment_client: DeploymentControlClient | None = field(default=None, init=False, repr=False)
    deployment_resource_client: DeploymentResourceClient | None = field(
        default=None,
        init=False,
        repr=False,
    )
    task_control_client: TaskControlClient | None = field(default=None, init=False, repr=False)
    compute_control_client: ComputeControlChannel | None = field(
        default=None,
        init=False,
        repr=False,
    )
    observability_client: ObservabilityClient | None = field(
        default=None,
        init=False,
        repr=False,
    )
    endpoint: str | None = field(default=None, init=False, repr=False)
    token: str | None = field(default=None, init=False, repr=False)
    timeout_seconds: float = field(default=10.0, init=False, repr=False)

    def _bind_control(
        self,
        *,
        deployment_client: DeploymentControlClient | None = None,
        deployment_resource_client: DeploymentResourceClient | None = None,
        task_control_client: TaskControlClient | None = None,
        compute_control_client: ComputeControlChannel | None = None,
        observability_client: ObservabilityClient | None = None,
        endpoint: str | None = None,
        token: str | None = None,
        timeout_seconds: float | None = None,
    ) -> Self:
        self.deployment_client = deployment_client
        self.deployment_resource_client = deployment_resource_client
        self.task_control_client = task_control_client
        self.compute_control_client = compute_control_client
        self.observability_client = observability_client
        if endpoint is not None:
            self.endpoint = endpoint
        if token is not None:
            self.token = token
        if timeout_seconds is not None:
            self.timeout_seconds = timeout_seconds
        return self

    @property
    def active_profile(self) -> ClientProfile:
        return self.profile or get_profile()

    def deployments(
        self,
        *,
        limit: int = 100,
    ) -> list[DeploymentResponse]:
        return self.deployment.list(limit=limit)

    @property
    def deployment(self) -> DeploymentClient:
        return DeploymentClient(
            client=self.deployment_client,
            resource_client=self.deployment_resource_client,
            workspace=self._config().workspace,
            endpoint=self.endpoint,
            token=self.token,
            timeout_seconds=self.timeout_seconds,
        )

    def get_deployment_by_id(self, deployment_id: str) -> DeploymentResponse:
        return self.deployment.get(deployment_id)

    def get_deployment_by_name(self, name: str) -> DeploymentResponse:
        return self.deployment.get(name)

    def deployment_handle(self, deployment_id_or_name: str) -> Deployment:
        return self.deployment.handle(deployment_id_or_name)

    def submit_deployment(
        self,
        deployment_id_or_name: str,
        *args: object,
        kwargs: dict[str, object] | None = None,
    ) -> DeploymentSubmission:
        return self.deployment.submit(
            deployment_id_or_name,
            *args,
            kwargs=kwargs,
        )

    def subscribe_deployment(
        self,
        deployment_id_or_name: str,
        *args: object,
        kwargs: dict[str, object] | None = None,
    ) -> TaskSubscription:
        return self.deployment.subscribe(deployment_id_or_name, *args, kwargs=kwargs)

    def tasks(
        self,
        *,
        status: TaskStatus | None = None,
        limit: int = 100,
    ) -> list[TaskResponse]:
        return self.task_client.list(status=status, limit=limit)

    @property
    def task_client(self) -> TaskClient:
        return TaskClient(
            client=self.task_control_client,
            observability_client=self.observability_client,
            workspace=self._config().workspace,
            endpoint=self.endpoint,
            token=self.token,
            timeout_seconds=self.timeout_seconds,
        )

    def task(self, task_id: str) -> TaskResponse:
        return self.task_client.get(task_id)

    def get_task_by_id(self, task_id: str) -> TaskResponse:
        return self.task(task_id)

    def task_handle(self, task_id: str) -> Task:
        return self.task_client.handle(task_id)

    def task_result(self, task_id: str, *, wait: bool = False) -> object:
        return self.task_client.result(task_id, wait=wait).value

    def task_output(self, task_id: str, *, limit: int = 100, page: int = 0) -> str:
        return self.task_client.output(task_id, limit=limit, page=page)

    def subscribe_task(self, task_id: str) -> TaskSubscription:
        return self.task_client.subscribe(task_id)

    @property
    def compute(self) -> ComputeClient:
        config = self._config()
        if self.compute_control_client is not None:
            return ComputeClient(
                channel=self.compute_control_client,
                workspace=config.workspace,
            )
        return ComputeClient.from_endpoint(
            config.endpoint,
            token=config.token,
            timeout_seconds=config.timeout_seconds,
            workspace=config.workspace,
        )

    def upload_bytes(
        self,
        data: bytes,
        *,
        name: str,
        bucket: str = "default",
        overwrite: bool = False,
        content_type: str = "application/octet-stream",
        metadata: dict[str, str] | None = None,
        progress: ProgressCallback | None = None,
    ) -> UploadedObject:
        object_hash = hashlib.sha256(data).hexdigest()
        config = self._config()
        upload_timeout_seconds = object_upload_timeout_seconds(self.timeout_seconds)
        response = stream_object_bytes(
            endpoint=config.endpoint,
            token=config.token,
            workspace=config.workspace,
            data=data,
            name=name,
            object_hash=object_hash,
            bucket=bucket,
            overwrite=overwrite,
            content_type=content_type,
            metadata=metadata,
            timeout_seconds=upload_timeout_seconds,
            progress=progress,
        )
        return UploadedObject(
            object_id=response.object_id,
            name=name,
            bucket=bucket,
            size=len(data),
            sha256=object_hash,
        )

    def upload_file(
        self,
        path: str | Path,
        *,
        name: str | None = None,
        bucket: str = "default",
        overwrite: bool = False,
        content_type: str | None = None,
        metadata: dict[str, str] | None = None,
        progress: ProgressCallback | None = None,
    ) -> UploadedObject:
        source = Path(path).expanduser().resolve()
        if not source.is_file():
            raise FileNotFoundError(source)
        size = source.stat().st_size
        digest = hashlib.sha256()
        with source.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        object_hash = digest.hexdigest()
        selected_name = name or source.name
        selected_content_type = (
            content_type or mimetypes.guess_type(selected_name)[0] or "application/octet-stream"
        )
        config = self._config()
        response = stream_object_file(
            endpoint=config.endpoint,
            token=config.token,
            workspace=config.workspace,
            source=source,
            size=size,
            name=selected_name,
            object_hash=object_hash,
            bucket=bucket,
            overwrite=overwrite,
            content_type=selected_content_type,
            metadata=metadata,
            timeout_seconds=object_upload_timeout_seconds(self.timeout_seconds),
            progress=progress,
        )
        return UploadedObject(
            object_id=response.object_id,
            name=selected_name,
            bucket=bucket,
            size=size,
            sha256=object_hash,
        )

    @property
    def observability(self) -> ObservabilityClient:
        if self.observability_client is None:
            self.observability_client = observability_control_client(self._config())
        return self.observability_client

    def events(
        self,
        *,
        limit: int | None = None,
        workspace: str | None = None,
    ) -> list[Event]:
        response = self.event_history(
            EventHistoryRequest(
                workspace_id=workspace or self._config().workspace,
                limit=limit if limit is not None else 100,
            )
        )
        return list(response.data)

    def event_history(self, request: EventHistoryRequest) -> EventQueryResponse:
        return self.observability.events(request)

    def _config(self) -> ControlClientConfig:
        return resolve_control_client_config(
            endpoint=self.endpoint,
            token=self.token,
            workspace=self.workspace or self.active_profile.workspace,
            timeout_seconds=self.timeout_seconds,
        )

    def _http_channel(self) -> HttpChannel:
        return control_http_channel(self._config())


__all__ = [
    "Client",
    "Deployment",
    "DeploymentClient",
    "DeploymentControlClient",
    "DeploymentSubmission",
    "FunctionCall",
    "Task",
    "TaskBatch",
    "TaskClient",
    "TaskControlClient",
    "TaskSubscription",
    "UploadedObject",
]
