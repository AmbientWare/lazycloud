"""HTTP client for the LazyCloud public API in contracts/openapi.yaml."""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, TypeVar, overload
from urllib.parse import quote
from uuid import UUID

import httpx
from pydantic import BaseModel, ValidationError
from shared.api import (
    App,
    AppPage,
    Container,
    ContainerPage,
    Deployment,
    DeploymentPlan,
    DeploymentPlanRequest,
    DeploymentRequest,
    DeviceLogin,
    DeviceLoginRequest,
    DeviceTokenRequest,
    DeviceTokenResponse,
    Error,
    ErrorCode,
    Image,
    ImageBuild,
    ImageBuildLogEntry,
    ImageDefinition,
    ImageResolution,
    LiveAppState,
    LogEntry,
    Me,
    Payload,
    Release,
    ScaleRequest,
    SchedulePage,
    Secret,
    SecretCreate,
    SecretPage,
    SecretValue,
    SecretValueUpdate,
    SourceUpload,
    SourceUploadRequest,
    StartWorkloadRequest,
    StopTasksRequest,
    StopTasksResponse,
    SubmitTasksRequest,
    SubmitTasksResponse,
    Task,
    TaskPage,
    TaskStatus,
    UploadTarget,
    Workload,
    WorkloadDetail,
    WorkloadKind,
    WorkloadPage,
    WorkloadSpec,
    Workspace,
    WorkspaceList,
    WorkspaceRequest,
)
from shared.client_version import (
    RECOMMENDED_CLIENT_VERSION_HEADER,
    client_version,
    report_client_version,
)
from shared.task_context import current_task_id

from lazycloud.exceptions import SdkError

ModelT = TypeVar("ModelT", bound=BaseModel)

# Added to a long-poll's own hold time so the read does not time out first.
_WAIT_READ_MARGIN_SECONDS = 15.0
_SOURCE_UPLOAD_TIMEOUT_SECONDS = 600.0
_UPLOAD_CHUNK_BYTES = 1024 * 1024


# Longer than the server's 15-second follow heartbeat.
_LOG_HEARTBEAT_WINDOW_SECONDS = 45.0

# Inside a container the API is a Unix socket; the host part is unused.
_CONTAINER_BASE_URL = "http://container"
# Names the task making a container API call, so a spawned task records it
# as parent. The server checks that the task runs on the calling container.
TASK_HEADER = "LazyCloud-Task"


class ApiError(SdkError):
    """The API answered with a typed error."""

    def __init__(self, *, status_code: int, code: ErrorCode | None, message: str) -> None:
        self.status_code = status_code
        self.code = code
        self.message = message
        label = code.value if code is not None else f"HTTP {status_code}"
        super().__init__(f"{label}: {message}")


class ApiConnectionError(SdkError):
    """A request failed before the API answered."""

    def __init__(self, method: str, path: str, reason: str) -> None:
        self.method = method
        self.path = path
        self.reason = reason
        super().__init__(f"{method} {path}: {reason}")


@dataclass
class ApiClient:
    """One connection pool to the API, authenticated when `token` is set.

    Workspace, app and function names are validated by the server; they are
    only escaped here. Device login runs before any token exists, so its two
    operations are the ones an empty token can call. With `container_api`
    set the client talks to the container API socket the platform serves
    inside every workload container; it carries no token, since the platform
    knows the container.
    """

    endpoint: str
    token: str | None = field(default=None, repr=False)
    timeout_seconds: float = 30.0
    container_api: str | None = None
    _http: httpx.Client | None = field(default=None, init=False, repr=False)

    def close(self) -> None:
        if self._http is not None:
            self._http.close()
            self._http = None

    def __enter__(self) -> ApiClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def me(self) -> Me:
        return self._send(Me, "GET", "/v1/me")

    def start_device_login(self, client_name: str) -> DeviceLogin:
        return self._send(
            DeviceLogin,
            "POST",
            "/v1/device-codes",
            body=DeviceLoginRequest(client_name=client_name),
        )

    def poll_device_login(self, device_code: str) -> DeviceTokenResponse:
        return self._send(
            DeviceTokenResponse,
            "POST",
            "/v1/device-codes/token",
            body=DeviceTokenRequest(device_code=device_code),
        )

    def list_workspaces(self) -> list[Workspace]:
        """Every workspace the caller may act on, following pages."""

        workspaces: list[Workspace] = []
        cursor: str | None = None
        while True:
            params: dict[str, str | int] = {"limit": 1000}
            if cursor:
                params["cursor"] = cursor
            page = self._send(WorkspaceList, "GET", "/v1/workspaces", params=params)
            workspaces.extend(page.workspaces)
            if not page.next_cursor:
                return workspaces
            cursor = page.next_cursor

    def get_workspace(self, name: str) -> Workspace:
        return self._send(Workspace, "GET", _path("v1", "workspaces", name))

    def create_workspace(self, name: str, *, cloud: Literal["aws"] | None = None) -> Workspace:
        """Create a workspace on LazyCloud, or in the connected AWS account with `cloud`."""
        request = WorkspaceRequest(name=name, cloud=cloud) if cloud else WorkspaceRequest(name=name)
        return self._send(Workspace, "POST", "/v1/workspaces", body=request)

    def rename_workspace(self, name: str, new_name: str) -> Workspace:
        return self._send(
            Workspace,
            "PATCH",
            _path("v1", "workspaces", name),
            body=WorkspaceRequest(name=new_name),
        )

    def delete_workspace(self, name: str) -> Workspace:
        """Begin deleting a workspace; it reports `deleting` until cleanup ends."""

        return self._send(Workspace, "DELETE", _path("v1", "workspaces", name))

    def create_source_upload(self, workspace: str, request: SourceUploadRequest) -> SourceUpload:
        return self._send(
            SourceUpload,
            "POST",
            _path("v1", "workspaces", workspace, "sources"),
            body=request,
        )

    def upload_source(
        self,
        target: UploadTarget,
        archive: Path | bytes,
        *,
        progress: Callable[[int], None] | None = None,
    ) -> None:
        """Send archive bytes to a presigned upload target.

        The target URL carries its own authorization, so the bearer token is
        not sent with it. `progress` receives the bytes read so far.
        """

        size = len(archive) if isinstance(archive, bytes) else archive.stat().st_size
        headers = {**target.headers, "Content-Length": str(size)}

        def chunks(path: Path) -> Iterator[bytes]:
            sent = 0
            with path.open("rb") as source:
                while chunk := source.read(_UPLOAD_CHUNK_BYTES):
                    yield chunk
                    sent += len(chunk)
                    if progress is not None:
                        progress(sent)

        try:
            response = httpx.request(
                target.method,
                target.url,
                headers=headers,
                content=archive if isinstance(archive, bytes) else chunks(archive),
                timeout=httpx.Timeout(self.timeout_seconds, write=_SOURCE_UPLOAD_TIMEOUT_SECONDS),
            )
        except httpx.HTTPError as exc:
            raise ApiConnectionError(
                "PUT", "source upload", str(exc) or type(exc).__name__
            ) from exc
        if response.status_code >= 300:
            raise ApiError(
                status_code=response.status_code,
                code=None,
                message=f"source upload was rejected: {response.text[:500]}",
            )

    def store_source(self, workspace: str, sha256: str, archive: Path | bytes) -> bool:
        """Make an archive present by digest; True when its bytes had to be sent."""
        size = len(archive) if isinstance(archive, bytes) else archive.stat().st_size
        request = SourceUploadRequest(sha256=sha256, size_bytes=size)
        state = self.create_source_upload(workspace, request)
        if state.present:
            return False
        if state.upload is None:
            raise ApiError(
                status_code=200,
                code=None,
                message=f"source {sha256} is missing and the API gave no upload target",
            )
        self.upload_source(state.upload, archive)
        if not self.create_source_upload(workspace, request).present:
            raise ApiError(
                status_code=200,
                code=None,
                message=f"source {sha256} was not stored after its upload",
            )
        return True

    def deploy_app(self, workspace: str, app: str, request: DeploymentRequest) -> Deployment:
        return self._send(
            Deployment,
            "POST",
            _path("v1", "workspaces", workspace, "apps", app, "deployments"),
            body=request,
        )

    def submit_tasks(
        self, workspace: str, app: str, function: str, request: SubmitTasksRequest
    ) -> SubmitTasksResponse:
        return self._send(
            SubmitTasksResponse,
            "POST",
            workload_path(workspace, app, WorkloadKind.function, function, "tasks"),
            body=request,
        )

    def get_task(self, workspace: str, task_id: UUID, *, wait_seconds: int = 0) -> Task:
        return self._send(
            Task,
            "GET",
            _path("v1", "workspaces", workspace, "tasks", str(task_id)),
            params={"wait_seconds": wait_seconds} if wait_seconds else None,
            read_timeout=wait_seconds + _WAIT_READ_MARGIN_SECONDS if wait_seconds else None,
        )

    def get_task_result(self, workspace: str, task_id: UUID) -> Payload:
        return self._send(
            Payload,
            "GET",
            _path("v1", "workspaces", workspace, "tasks", str(task_id), "result"),
        )

    def cancel_task(self, workspace: str, task_id: UUID) -> Task:
        return self._send(
            Task,
            "POST",
            _path("v1", "workspaces", workspace, "tasks", str(task_id), "cancel"),
        )

    def list_tasks(
        self,
        workspace: str,
        *,
        app: str | None = None,
        function: str | None = None,
        status: TaskStatus | None = None,
        limit: int = 100,
        cursor: str | None = None,
    ) -> TaskPage:
        params = _query(
            app=app,
            function=function,
            status=status.value if status is not None else None,
            limit=limit,
            cursor=cursor,
        )
        return self._send(
            TaskPage, "GET", _path("v1", "workspaces", workspace, "tasks"), params=params
        )

    def stop_tasks(self, workspace: str, task_ids: Sequence[UUID]) -> StopTasksResponse:
        return self._send(
            StopTasksResponse,
            "POST",
            _path("v1", "workspaces", workspace, "tasks", "stop"),
            body=StopTasksRequest(task_ids=list(task_ids)),
        )

    def rerun_task(self, workspace: str, task_id: UUID) -> Task:
        return self._send(
            Task, "POST", _path("v1", "workspaces", workspace, "tasks", str(task_id), "rerun")
        )

    def list_apps(
        self,
        workspace: str,
        *,
        state: LiveAppState | None = None,
        limit: int = 100,
        cursor: str | None = None,
    ) -> AppPage:
        params = _query(
            state=state.value if state is not None else None, limit=limit, cursor=cursor
        )
        return self._send(
            AppPage, "GET", _path("v1", "workspaces", workspace, "apps"), params=params
        )

    def get_app(self, workspace: str, app: str) -> App:
        """Read an app by name or id."""
        return self._send(App, "GET", _path("v1", "workspaces", workspace, "apps", app))

    def pause_app(self, workspace: str, app: str) -> App:
        return self._send(App, "POST", _path("v1", "workspaces", workspace, "apps", app, "pause"))

    def resume_app(self, workspace: str, app: str) -> App:
        return self._send(App, "POST", _path("v1", "workspaces", workspace, "apps", app, "resume"))

    def delete_app(self, workspace: str, app: str) -> App:
        return self._send(App, "DELETE", _path("v1", "workspaces", workspace, "apps", app))

    def plan_deployment(
        self, workspace: str, app: str, request: DeploymentPlanRequest
    ) -> DeploymentPlan:
        return self._send(
            DeploymentPlan,
            "POST",
            _path("v1", "workspaces", workspace, "apps", app, "deployment-plan"),
            body=request,
        )

    def prepare_release(self, workspace: str, app: str, spec: WorkloadSpec) -> Release:
        return self._send(
            Release,
            "POST",
            _path("v1", "workspaces", workspace, "apps", app, "releases"),
            body=spec,
        )

    def list_workloads(
        self,
        workspace: str,
        *,
        app: str | None = None,
        kind: WorkloadKind | None = None,
        name: str | None = None,
        workload_id: UUID | None = None,
        limit: int = 100,
        cursor: str | None = None,
    ) -> WorkloadPage:
        params = _query(
            app=app,
            kind=kind.value if kind is not None else None,
            name=name,
            id=str(workload_id) if workload_id is not None else None,
            limit=limit,
            cursor=cursor,
        )
        return self._send(
            WorkloadPage, "GET", _path("v1", "workspaces", workspace, "workloads"), params=params
        )

    def get_workload(
        self, workspace: str, app: str, kind: WorkloadKind, name: str, *, version: int | None = None
    ) -> WorkloadDetail:
        """The live workload and its active release, or the release of `version`."""
        return self._send(
            WorkloadDetail,
            "GET",
            workload_path(workspace, app, kind, name),
            params=_query(version=version),
        )

    def stop_workload(self, workspace: str, app: str, kind: WorkloadKind, name: str) -> Workload:
        return self._send(Workload, "POST", workload_path(workspace, app, kind, name, "stop"))

    def start_workload(
        self, workspace: str, app: str, kind: WorkloadKind, name: str, *, version: int | None = None
    ) -> Workload:
        return self._send(
            Workload,
            "POST",
            workload_path(workspace, app, kind, name, "start"),
            body=StartWorkloadRequest(version=version) if version is not None else None,
        )

    def scale_workload(
        self, workspace: str, app: str, kind: WorkloadKind, name: str, containers: int
    ) -> Workload:
        """Hold a pod at `containers` containers until the next scale."""
        return self._send(
            Workload,
            "POST",
            workload_path(workspace, app, kind, name, "scale"),
            body=ScaleRequest(containers=containers),
        )

    def delete_workload(self, workspace: str, app: str, kind: WorkloadKind, name: str) -> Workload:
        return self._send(Workload, "DELETE", workload_path(workspace, app, kind, name))

    def list_containers(
        self,
        workspace: str,
        *,
        live: bool = False,
        app: str | None = None,
        limit: int = 100,
        cursor: str | None = None,
    ) -> ContainerPage:
        params = _query(live="true" if live else None, app=app, limit=limit, cursor=cursor)
        return self._send(
            ContainerPage, "GET", _path("v1", "workspaces", workspace, "containers"), params=params
        )

    def get_container(self, workspace: str, container_id: UUID) -> Container:
        return self._send(
            Container,
            "GET",
            _path("v1", "workspaces", workspace, "containers", str(container_id)),
        )

    def stop_container(self, workspace: str, container_id: UUID) -> Container:
        return self._send(
            Container,
            "POST",
            _path("v1", "workspaces", workspace, "containers", str(container_id), "stop"),
        )

    def stream_task_logs(
        self,
        workspace: str,
        task_id: UUID,
        *,
        after: int = 0,
        tail: int | None = None,
        follow: bool = False,
    ) -> Iterator[LogEntry]:
        """Yield the task's log entries with ids above `after`.

        With `tail` the stream starts at the last `tail` stored entries. With
        `follow` the server holds the stream open until the task finishes and
        writes a blank line at least every 15 seconds, so a read that waits
        longer than the heartbeat window means the connection is gone.
        """

        return self._stream_logs(
            _path("v1", "workspaces", workspace, "tasks", str(task_id), "logs"),
            after=after,
            tail=tail,
            follow=follow,
        )

    def stream_workload_logs(
        self,
        workspace: str,
        app: str,
        kind: WorkloadKind,
        name: str,
        *,
        after: int = 0,
        tail: int | None = None,
        follow: bool = False,
    ) -> Iterator[LogEntry]:
        """Yield log entries of every task of a workload; a followed stream never ends."""

        return self._stream_logs(
            workload_path(workspace, app, kind, name, "logs"),
            after=after,
            tail=tail,
            follow=follow,
        )

    def stream_container_logs(
        self,
        workspace: str,
        container_id: UUID,
        *,
        after: int = 0,
        tail: int | None = None,
        follow: bool = False,
    ) -> Iterator[LogEntry]:
        """Yield log entries of the tasks a container ran; following ends when it stops."""

        return self._stream_logs(
            _path("v1", "workspaces", workspace, "containers", str(container_id), "logs"),
            after=after,
            tail=tail,
            follow=follow,
        )

    def resolve_image(self, workspace: str, definition: ImageDefinition) -> ImageResolution:
        """The image a definition names and any active build; never starts one."""
        return self._send(
            ImageResolution,
            "POST",
            _path("v1", "workspaces", workspace, "images", "resolve"),
            body=definition,
        )

    def build_image(
        self, workspace: str, definition: ImageDefinition, *, force: bool = False
    ) -> ImageResolution:
        """Start or join the image's build unless it is ready; `force` builds again."""
        return self._send(
            ImageResolution,
            "POST",
            _path("v1", "workspaces", workspace, "images"),
            body=definition,
            params={"force": "true"} if force else None,
        )

    def get_image(self, workspace: str, image_id: str) -> Image:
        return self._send(Image, "GET", _path("v1", "workspaces", workspace, "images", image_id))

    def get_image_build(
        self, workspace: str, build_id: UUID, *, wait_seconds: int = 0
    ) -> ImageBuild:
        return self._send(
            ImageBuild,
            "GET",
            _path("v1", "workspaces", workspace, "image-builds", str(build_id)),
            params={"wait_seconds": wait_seconds} if wait_seconds else None,
            read_timeout=wait_seconds + _WAIT_READ_MARGIN_SECONDS if wait_seconds else None,
        )

    def stream_image_build_logs(
        self,
        workspace: str,
        build_id: UUID,
        *,
        after: int = 0,
        follow: bool = False,
    ) -> Iterator[ImageBuildLogEntry]:
        """Yield build output with ids above `after`, as `stream_task_logs` does."""
        path = _path("v1", "workspaces", workspace, "image-builds", str(build_id), "logs")
        return self._stream_lines(ImageBuildLogEntry, path, after=after, tail=None, follow=follow)

    def _stream_logs(
        self, path: str, *, after: int, tail: int | None, follow: bool
    ) -> Iterator[LogEntry]:
        return self._stream_lines(LogEntry, path, after=after, tail=tail, follow=follow)

    def _stream_lines(
        self, model: type[ModelT], path: str, *, after: int, tail: int | None, follow: bool
    ) -> Iterator[ModelT]:
        params: dict[str, str | int] = {"after": after}
        if tail is not None:
            params["tail"] = tail
        if follow:
            params["follow"] = "true"
        read = self.timeout_seconds
        if follow:
            read = max(read, _LOG_HEARTBEAT_WINDOW_SECONDS)
        timeout = httpx.Timeout(self.timeout_seconds, read=read)
        try:
            with self._client().stream("GET", path, params=params, timeout=timeout) as response:
                report_client_version(response.headers.get(RECOMMENDED_CLIENT_VERSION_HEADER))
                if response.status_code >= 300:
                    response.read()
                    raise _api_error(response)
                for line in response.iter_lines():
                    if not line.strip():
                        continue
                    try:
                        yield model.model_validate_json(line)
                    except ValidationError as exc:
                        raise ApiError(
                            status_code=response.status_code,
                            code=None,
                            message=f"invalid log entry from {path}: {exc}",
                        ) from exc
        except httpx.HTTPError as exc:
            raise ApiConnectionError("GET", path, str(exc) or type(exc).__name__) from exc

    def list_secrets(
        self, workspace: str, *, cursor: str | None = None, limit: int | None = None
    ) -> SecretPage:
        params: dict[str, str | int] = {}
        if cursor:
            params["cursor"] = cursor
        if limit is not None:
            params["limit"] = limit
        return self._send(
            SecretPage, "GET", _path("v1", "workspaces", workspace, "secrets"), params=params
        )

    def create_secret(self, workspace: str, name: str, value: str) -> Secret:
        return self._send(
            Secret,
            "POST",
            _path("v1", "workspaces", workspace, "secrets"),
            body=SecretCreate(name=name, value=value),
        )

    def get_secret(self, workspace: str, name: str) -> Secret:
        return self._send(Secret, "GET", _path("v1", "workspaces", workspace, "secrets", name))

    def set_secret(self, workspace: str, name: str, value: str) -> Secret:
        return self._send(
            Secret,
            "PUT",
            _path("v1", "workspaces", workspace, "secrets", name),
            body=SecretValueUpdate(value=value),
        )

    def update_secret(self, workspace: str, name: str, value: str) -> Secret:
        return self._send(
            Secret,
            "PATCH",
            _path("v1", "workspaces", workspace, "secrets", name),
            body=SecretValueUpdate(value=value),
        )

    def delete_secret(self, workspace: str, name: str) -> None:
        self._send(None, "DELETE", _path("v1", "workspaces", workspace, "secrets", name))

    def get_secret_value(self, workspace: str, name: str) -> SecretValue:
        return self._send(
            SecretValue, "GET", _path("v1", "workspaces", workspace, "secrets", name, "value")
        )

    def list_schedules(
        self, workspace: str, *, cursor: str | None = None, limit: int | None = None
    ) -> SchedulePage:
        params: dict[str, str | int] = {}
        if cursor:
            params["cursor"] = cursor
        if limit is not None:
            params["limit"] = limit
        return self._send(
            SchedulePage, "GET", _path("v1", "workspaces", workspace, "schedules"), params=params
        )

    def _client(self) -> httpx.Client:
        if self._http is None:
            headers = {"User-Agent": f"lazycloud/{client_version()}"}
            if self.container_api:
                self._http = httpx.Client(
                    base_url=_CONTAINER_BASE_URL,
                    headers=headers,
                    transport=httpx.HTTPTransport(uds=self.container_api),
                    timeout=self.timeout_seconds,
                    event_hooks={"request": [_name_calling_task]},
                )
                return self._http
            if self.token:
                headers["Authorization"] = f"Bearer {self.token}"
            self._http = httpx.Client(
                base_url=self.endpoint.rstrip("/"),
                headers=headers,
                timeout=self.timeout_seconds,
            )
        return self._http

    @overload
    def _send(
        self,
        model: type[ModelT],
        method: str,
        path: str,
        *,
        body: BaseModel | None = None,
        params: Mapping[str, str | int] | None = None,
        read_timeout: float | None = None,
    ) -> ModelT: ...

    @overload
    def _send(
        self,
        model: None,
        method: str,
        path: str,
        *,
        body: BaseModel | None = None,
        params: Mapping[str, str | int] | None = None,
        read_timeout: float | None = None,
    ) -> None: ...

    def _send(
        self,
        model: type[ModelT] | None,
        method: str,
        path: str,
        *,
        body: BaseModel | None = None,
        params: Mapping[str, str | int] | None = None,
        read_timeout: float | None = None,
    ) -> ModelT | None:
        content = (
            body.model_dump_json(exclude_unset=True, by_alias=True).encode()
            if body is not None
            else None
        )
        timeout = (
            httpx.Timeout(self.timeout_seconds, read=read_timeout)
            if read_timeout is not None
            else None
        )
        try:
            response = self._client().request(
                method,
                path,
                content=content,
                params=params,
                headers={"Content-Type": "application/json"} if content is not None else None,
                timeout=timeout if timeout is not None else httpx.USE_CLIENT_DEFAULT,
            )
        except httpx.HTTPError as exc:
            raise ApiConnectionError(method, path, str(exc) or type(exc).__name__) from exc
        report_client_version(response.headers.get(RECOMMENDED_CLIENT_VERSION_HEADER))
        if response.status_code >= 300:
            raise _api_error(response)
        if model is None:
            return None
        try:
            return model.model_validate_json(response.content)
        except ValidationError as exc:
            raise ApiError(
                status_code=response.status_code,
                code=None,
                message=f"{method} {path} returned an invalid {model.__name__}: {exc}",
            ) from exc


def _name_calling_task(request: httpx.Request) -> None:
    """Name the task this thread or coroutine runs for, if any."""

    task_id = current_task_id()
    if task_id:
        request.headers[TASK_HEADER] = task_id


def is_transient(error: Exception) -> bool:
    """Whether retrying the same request can succeed without changing it."""

    if isinstance(error, ApiConnectionError):
        return True
    return isinstance(error, ApiError) and error.status_code in {502, 503, 504}


def _api_error(response: httpx.Response) -> ApiError:
    try:
        error = Error.model_validate_json(response.content)
    except ValidationError:
        text = response.text.strip()[:500] or response.reason_phrase
        return ApiError(status_code=response.status_code, code=None, message=text)
    return ApiError(status_code=response.status_code, code=error.code, message=error.message)


def _query(**values: str | int | None) -> dict[str, str | int]:
    return {name: value for name, value in values.items() if value is not None}


def _path(*segments: str) -> str:
    return "/" + "/".join(quote(segment, safe="") for segment in segments)


def workload_path(workspace: str, app: str, kind: WorkloadKind, name: str, *segments: str) -> str:
    """The API path of one workload, or of `segments` beneath it."""
    return _path(
        "v1", "workspaces", workspace, "apps", app, "workloads", kind.value, name, *segments
    )


__all__ = ["ApiClient", "ApiConnectionError", "ApiError", "is_transient"]
