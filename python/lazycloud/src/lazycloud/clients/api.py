"""HTTP client for the LazyCloud public API in contracts/openapi.yaml."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import TypeVar
from urllib.parse import quote
from uuid import UUID

import httpx
from pydantic import BaseModel, ValidationError
from shared.api import (
    Deployment,
    DeploymentRequest,
    Error,
    ErrorCode,
    Function,
    LogEntry,
    Me,
    Payload,
    SourceUpload,
    SourceUploadRequest,
    SubmitTasksRequest,
    SubmitTasksResponse,
    Task,
    UploadTarget,
)

from lazycloud.exceptions import SdkError

ModelT = TypeVar("ModelT", bound=BaseModel)

# Added to a long-poll's own hold time so the read does not time out first.
_WAIT_READ_MARGIN_SECONDS = 15.0
_SOURCE_UPLOAD_TIMEOUT_SECONDS = 600.0
_UPLOAD_CHUNK_BYTES = 1024 * 1024


# Longer than the server's 15-second follow heartbeat.
_LOG_HEARTBEAT_WINDOW_SECONDS = 45.0


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
    """One authenticated connection pool to the API.

    Workspace, app and function names are validated by the server; they are
    only escaped here.
    """

    endpoint: str
    token: str = field(repr=False)
    timeout_seconds: float = 30.0
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

    def create_source_upload(self, workspace: str, request: SourceUploadRequest) -> SourceUpload:
        return self._send(
            SourceUpload,
            "POST",
            _path("v1", "workspaces", workspace, "sources"),
            body=request,
        )

    def upload_source(self, target: UploadTarget, archive: Path) -> None:
        """Send archive bytes to a presigned upload target.

        The target URL carries its own authorization, so the bearer token is
        not sent with it.
        """

        size = archive.stat().st_size
        headers = {**target.headers, "Content-Length": str(size)}

        def chunks() -> Iterator[bytes]:
            with archive.open("rb") as source:
                while chunk := source.read(_UPLOAD_CHUNK_BYTES):
                    yield chunk

        try:
            response = httpx.request(
                target.method,
                target.url,
                headers=headers,
                content=chunks(),
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

    def deploy_app(self, workspace: str, app: str, request: DeploymentRequest) -> Deployment:
        return self._send(
            Deployment,
            "POST",
            _path("v1", "workspaces", workspace, "apps", app, "deployments"),
            body=request,
        )

    def get_function(self, workspace: str, app: str, function: str) -> Function:
        return self._send(
            Function,
            "GET",
            _path("v1", "workspaces", workspace, "apps", app, "functions", function),
        )

    def submit_tasks(
        self, workspace: str, app: str, function: str, request: SubmitTasksRequest
    ) -> SubmitTasksResponse:
        return self._send(
            SubmitTasksResponse,
            "POST",
            _path("v1", "workspaces", workspace, "apps", app, "functions", function, "tasks"),
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

    def stream_task_logs(
        self,
        workspace: str,
        task_id: UUID,
        *,
        after: int = 0,
        follow: bool = False,
    ) -> Iterator[LogEntry]:
        """Yield log entries with ids above `after`.

        With `follow` the server holds the stream open until the task finishes
        and writes a blank line at least every 15 seconds, so a read that waits
        longer than the heartbeat window means the connection is gone.
        """

        path = _path("v1", "workspaces", workspace, "tasks", str(task_id), "logs")
        params: dict[str, str | int] = {"after": after}
        if follow:
            params["follow"] = "true"
        read = self.timeout_seconds
        if follow:
            read = max(read, _LOG_HEARTBEAT_WINDOW_SECONDS)
        timeout = httpx.Timeout(self.timeout_seconds, read=read)
        try:
            with self._client().stream("GET", path, params=params, timeout=timeout) as response:
                if response.status_code >= 300:
                    response.read()
                    raise _api_error(response)
                for line in response.iter_lines():
                    if not line.strip():
                        continue
                    try:
                        yield LogEntry.model_validate_json(line)
                    except ValidationError as exc:
                        raise ApiError(
                            status_code=response.status_code,
                            code=None,
                            message=f"invalid log entry from {path}: {exc}",
                        ) from exc
        except httpx.HTTPError as exc:
            raise ApiConnectionError("GET", path, str(exc) or type(exc).__name__) from exc

    def _client(self) -> httpx.Client:
        if self._http is None:
            self._http = httpx.Client(
                base_url=self.endpoint.rstrip("/"),
                headers={"Authorization": f"Bearer {self.token}"},
                timeout=self.timeout_seconds,
            )
        return self._http

    def _send(
        self,
        model: type[ModelT],
        method: str,
        path: str,
        *,
        body: BaseModel | None = None,
        params: dict[str, int] | None = None,
        read_timeout: float | None = None,
    ) -> ModelT:
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
        if response.status_code >= 300:
            raise _api_error(response)
        try:
            return model.model_validate_json(response.content)
        except ValidationError as exc:
            raise ApiError(
                status_code=response.status_code,
                code=None,
                message=f"{method} {path} returned an invalid {model.__name__}: {exc}",
            ) from exc


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


def _path(*segments: str) -> str:
    return "/" + "/".join(quote(segment, safe="") for segment in segments)


__all__ = ["ApiClient", "ApiConnectionError", "ApiError", "is_transient"]
