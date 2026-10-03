"""Handles through which packages from `lazycloud app export` call deployed workloads.

A generated package holds each workload's manifest and no credentials; a
handle signs requests with the caller's own profile token.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any, cast
from uuid import UUID

from pydantic import BaseModel, ConfigDict
from typing_extensions import assert_never

from lazycloud._shared.enums import StringEnum
from lazycloud._shared.http.client_manifests import ClientContract
from lazycloud._shared.serialization import to_json_value
from lazycloud.abstractions.http_calls import EndpointResponse, request_timeout, send_request
from lazycloud.clients.api import ApiClient, ApiConnectionError, ApiError
from lazycloud.contracts.api import (
    Encoding,
    ErrorCode,
    Payload,
    RunTaskRequest,
    TaskInput,
    TaskStatus,
)
from lazycloud.contracts.api import Task as TaskView
from lazycloud.control import api_client, resolve_control_client_config
from lazycloud.exceptions import FunctionNotDeployedError, SdkError
from lazycloud.json_contracts import JsonValue
from lazycloud.session.task import TERMINAL_STATUSES, Task


class ClientHandleError(SdkError):
    pass


class ResourceKind(StringEnum):
    Function = "function"
    Endpoint = "endpoint"
    Asgi = "asgi"


class ResourceManifest(BaseModel):
    """One exported workload as the package and its lock file record it.

    HTTP workloads also record the URL that follows their active release,
    the methods an endpoint answers, whether requests need the caller's
    token, and the timeout that bounds a request.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    app: str
    name: str
    kind: ResourceKind
    deployment_id: UUID
    deployment_version: int
    release_id: UUID
    client_contract: ClientContract
    url: str | None = None
    methods: tuple[str, ...] = ()
    authorized: bool = True
    timeout_seconds: int | None = None


@dataclass(slots=True)
class FunctionHandle:
    """Runs JSON tasks on a function's active release and returns their results."""

    manifest: ResourceManifest
    endpoint: str
    workspace: str
    _client: ApiClient | None = field(default=None, init=False, repr=False)

    @property
    def app(self) -> str:
        return self.manifest.app

    @property
    def name(self) -> str:
        return self.manifest.name

    def remote(self, *args: Any, **kwargs: Any) -> JsonValue:
        return self.remote_json(*args, **kwargs)

    def remote_json(self, *args: Any, **kwargs: Any) -> JsonValue:
        client = self._api()
        request = RunTaskRequest(
            input=TaskInput(
                encoding=Encoding.json,
                value={
                    "args": [_json_argument(item) for item in args],
                    "kwargs": {key: _json_argument(item) for key, item in kwargs.items()},
                },
            )
        )
        events = client.run_task(self.workspace, self.app, self.name, request)
        view: TaskView | None = None
        payload: Payload | None = None
        try:
            for event in events:
                if event.task is not None:
                    view, payload = event.task, event.result
        except ApiError as exc:
            if exc.code is ErrorCode.not_found:
                raise FunctionNotDeployedError(self.app, self.name, self.workspace) from exc
            raise
        except ApiConnectionError:
            # A stream that drops after admission is waited on below.
            if view is None:
                raise
        finally:
            events.close()
        if view is None:
            raise ClientHandleError("the run stream ended before it named the admitted task")
        task = Task(str(view.id), self.workspace, client)
        if view.status not in TERMINAL_STATUSES:
            view = task.wait_view()
        if view.status is not TaskStatus.succeeded:
            # Raises the remote exception, a typed failure or the cancellation.
            return task.outcome(view)
        if payload is None:
            payload = client.get_task_result(self.workspace, view.id)
        if payload.encoding is not Encoding.json:
            msg = f"task {task.task_id} returned a {payload.encoding.value} result, not JSON"
            raise ClientHandleError(msg)
        return payload.value

    async def async_remote(self, *args: Any, **kwargs: Any) -> JsonValue:
        return await asyncio.to_thread(self.remote_json, *args, **kwargs)

    async def async_remote_json(self, *args: Any, **kwargs: Any) -> JsonValue:
        return await asyncio.to_thread(self.remote_json, *args, **kwargs)

    def _api(self) -> ApiClient:
        if self._client is None:
            config = resolve_control_client_config(endpoint=self.endpoint, workspace=self.workspace)
            self._client = api_client(config)
        return self._client


@dataclass(slots=True)
class _HttpHandle:
    manifest: ResourceManifest
    endpoint: str
    workspace: str

    @property
    def app(self) -> str:
        return self.manifest.app

    @property
    def name(self) -> str:
        return self.manifest.name

    def _send(
        self,
        *,
        method: str,
        path: str = "",
        json_body: object | None = None,
        data: bytes | str | None = None,
        headers: Mapping[str, str] | None = None,
        params: Mapping[str, object] | Iterable[tuple[str, object]] | None = None,
    ) -> EndpointResponse:
        url = self.manifest.url
        if url is None:
            msg = f"{self.manifest.kind.value} {self.name} was exported without a URL"
            raise ClientHandleError(msg)
        # A public workload never receives the platform credential.
        token = (
            resolve_control_client_config(endpoint=self.endpoint, workspace=self.workspace).token
            if self.manifest.authorized
            else None
        )
        return send_request(
            url,
            method=method,
            path=path,
            json_body=json_body,
            data=data,
            headers=headers,
            params=params,
            token=token,
            timeout_seconds=request_timeout(self.manifest.timeout_seconds),
        )


class EndpointHandle(_HttpHandle):
    """Sends the call's arguments as the JSON body of one endpoint request."""

    def request(self, *args: Any, **kwargs: Any) -> EndpointResponse:
        allowed = [method.upper() for method in self.manifest.methods]
        method = "POST" if "POST" in allowed or not allowed else allowed[0]
        return self._send(
            method=method,
            json_body={
                "args": [_json_argument(item) for item in args],
                "kwargs": {key: _json_argument(item) for key, item in kwargs.items()},
            },
        )

    async def async_request(self, *args: Any, **kwargs: Any) -> EndpointResponse:
        return await asyncio.to_thread(self.request, *args, **kwargs)


class ASGIHandle(_HttpHandle):
    """Sends one request to a route of an ASGI app."""

    def request(
        self,
        *,
        method: str = "POST",
        path: str = "",
        json: object | None = None,
        data: bytes | str | None = None,
        headers: Mapping[str, str] | None = None,
        params: Mapping[str, object] | Iterable[tuple[str, object]] | None = None,
    ) -> EndpointResponse:
        return self._send(
            method=method, path=path, json_body=json, data=data, headers=headers, params=params
        )

    async def async_request(
        self,
        *,
        method: str = "POST",
        path: str = "",
        json: object | None = None,
        data: bytes | str | None = None,
        headers: Mapping[str, str] | None = None,
        params: Mapping[str, object] | Iterable[tuple[str, object]] | None = None,
    ) -> EndpointResponse:
        return await asyncio.to_thread(
            self.request,
            method=method,
            path=path,
            json=json,
            data=data,
            headers=headers,
            params=params,
        )


def handle_from_manifest(
    manifest: ResourceManifest | Mapping[str, JsonValue],
    *,
    endpoint: str,
    workspace: str,
) -> FunctionHandle | EndpointHandle | ASGIHandle:
    selected = (
        manifest
        if isinstance(manifest, ResourceManifest)
        else ResourceManifest.model_validate(manifest)
    )
    if selected.kind is ResourceKind.Function:
        return FunctionHandle(selected, endpoint=endpoint, workspace=workspace)
    if selected.kind is ResourceKind.Endpoint:
        return EndpointHandle(selected, endpoint=endpoint, workspace=workspace)
    if selected.kind is ResourceKind.Asgi:
        return ASGIHandle(selected, endpoint=endpoint, workspace=workspace)
    assert_never(selected.kind)


def _json_argument(value: object) -> JsonValue:
    """One argument as the JSON its schema describes: models by alias, sets as arrays."""
    if isinstance(value, BaseModel):
        return to_json_value(value.model_dump(mode="json", by_alias=True))
    if isinstance(value, list | tuple | set | frozenset):
        return [_json_argument(item) for item in cast("Iterable[object]", value)]
    if isinstance(value, Mapping):
        entries = cast("Mapping[object, object]", value)
        return {str(key): _json_argument(item) for key, item in entries.items()}
    return to_json_value(value)


__all__ = [
    "ASGIHandle",
    "ClientHandleError",
    "EndpointHandle",
    "FunctionHandle",
    "ResourceKind",
    "ResourceManifest",
    "handle_from_manifest",
]
