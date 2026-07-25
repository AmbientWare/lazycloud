from __future__ import annotations

import asyncio
import urllib.parse
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from pydantic import ValidationError
from shared.deployments import DeploymentKind
from shared.http.errors import http_api_error_from_body
from shared.http.functions import FunctionInvokeResponse
from shared.http.taskqueues import TaskQueuePutResponse
from shared.serialization import to_json_value
from typing_extensions import Self

from lazycloud.abstractions.endpoint import EndpointResponse
from lazycloud.config import get_profile
from lazycloud.function_results import FunctionResultDecodeError, decode_function_result
from lazycloud.http_transport import request_raw
from lazycloud.json_contracts import (
    JsonValue,
    parse_json_object,
)
from lazycloud.session.task import Task, TaskBatch, TaskClient


class ClientHandleError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class ResourceManifest:
    app: str
    name: str
    kind: DeploymentKind
    stub_id: str
    deployment_id: str
    deployment_version: int
    invoke_url: str
    route: str | None = None
    methods: tuple[str, ...] = ()
    inputs: dict[str, JsonValue] = field(default_factory=dict)
    outputs: dict[str, JsonValue] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: Mapping[str, JsonValue]) -> ResourceManifest:
        return cls(
            app=str(data["app"]),
            name=str(data["name"]),
            kind=DeploymentKind(str(data["kind"])),
            stub_id=str(data["stub_id"]),
            deployment_id=str(data["deployment_id"]),
            deployment_version=_manifest_int(data, "deployment_version"),
            invoke_url=str(data["invoke_url"]),
            route=str(data["route"]) if data.get("route") is not None else None,
            methods=_manifest_methods(data),
            inputs=_manifest_object(data, "inputs"),
            outputs=_manifest_object(data, "outputs"),
        )


@dataclass(slots=True)
class ResourceHandle:
    manifest: ResourceManifest
    token: str | None = field(default=None, init=False, repr=False)
    timeout_seconds: float = field(default=10.0, init=False, repr=False)

    @property
    def app(self) -> str:
        return self.manifest.app

    @property
    def name(self) -> str:
        return self.manifest.name

    @property
    def kind(self) -> DeploymentKind:
        return self.manifest.kind

    @property
    def stub_id(self) -> str:
        return self.manifest.stub_id

    @property
    def deployment_id(self) -> str:
        return self.manifest.deployment_id

    @property
    def deployment_version(self) -> int:
        return self.manifest.deployment_version

    @property
    def invoke_url(self) -> str:
        return self.manifest.invoke_url

    def _token(self) -> str | None:
        return self.token if self.token is not None else get_profile().token or None

    def _bind_control(
        self,
        *,
        token: str | None = None,
        timeout_seconds: float | None = None,
    ) -> Self:
        if token is not None:
            self.token = token
        if timeout_seconds is not None:
            self.timeout_seconds = timeout_seconds
        return self


class FunctionHandle(ResourceHandle):
    def remote(self, *args: Any, **kwargs: Any) -> Any:
        try:
            response = FunctionInvokeResponse.model_validate(
                _json_request(
                    self.invoke_url,
                    method="POST",
                    json_body=_call_payload(args, kwargs),
                    token=self._token(),
                    timeout_seconds=self.timeout_seconds,
                )
            )
        except ValidationError as exc:
            raise ClientHandleError("function returned an invalid response") from exc
        if response.exit_code != 0:
            raise ClientHandleError(response.output or "function invocation failed")
        if response.result is not None:
            try:
                return decode_function_result(response.result)
            except FunctionResultDecodeError as exc:
                raise ClientHandleError("function returned an invalid result") from exc
        return response.task_id or None

    def remote_json(self, *args: Any, **kwargs: Any) -> Any:
        try:
            response = FunctionInvokeResponse.model_validate(
                _json_request(
                    self.invoke_url,
                    method="POST",
                    json_body=_call_payload(args, kwargs, result_format="json"),
                    token=self._token(),
                    timeout_seconds=self.timeout_seconds,
                )
            )
        except ValidationError as exc:
            raise ClientHandleError("function returned an invalid response") from exc
        if response.exit_code != 0:
            raise ClientHandleError(response.output or "function invocation failed")
        if response.result is not None:
            try:
                return decode_function_result(response.result)
            except FunctionResultDecodeError as exc:
                raise ClientHandleError("function returned an invalid result") from exc
        if not response.task_id:
            return None
        completed = Task(
            task_id=response.task_id,
            client=TaskClient(
                endpoint=_origin(self.invoke_url),
                token=self._token(),
                timeout_seconds=self.timeout_seconds,
            ),
        ).wait()
        try:
            return decode_function_result(completed.value)
        except FunctionResultDecodeError as exc:
            raise ClientHandleError("function returned an invalid result") from exc

    async def async_remote(self, *args: Any, **kwargs: Any) -> Any:
        return await asyncio.to_thread(self.remote, *args, **kwargs)

    async def async_remote_json(self, *args: Any, **kwargs: Any) -> Any:
        return await asyncio.to_thread(self.remote_json, *args, **kwargs)


class EndpointHandle(ResourceHandle):
    def request(self, *args: Any, **kwargs: Any) -> EndpointResponse:
        return _endpoint_request(
            self.invoke_url,
            method=_endpoint_method(self.manifest),
            json_body=_call_payload(args, kwargs),
            token=self._token(),
            timeout_seconds=self.timeout_seconds,
        )

    async def async_request(self, *args: Any, **kwargs: Any) -> EndpointResponse:
        return await asyncio.to_thread(self.request, *args, **kwargs)


class ASGIHandle(ResourceHandle):
    def request(
        self,
        *,
        method: str = "POST",
        path: str = "",
        json: object | None = None,
        data: bytes | str | None = None,
        headers: dict[str, str] | None = None,
        params: dict[str, object] | list[tuple[str, object]] | None = None,
    ) -> EndpointResponse:
        return _endpoint_request(
            self.invoke_url,
            method=method,
            path=path,
            json_body=json,
            data=data,
            headers=headers,
            params=params,
            token=self._token(),
            timeout_seconds=self.timeout_seconds,
        )

    async def async_request(
        self,
        *,
        method: str = "POST",
        path: str = "",
        json: object | None = None,
        data: bytes | str | None = None,
        headers: dict[str, str] | None = None,
        params: dict[str, object] | list[tuple[str, object]] | None = None,
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


class TaskQueueHandle(ResourceHandle):
    def put(self, *args: Any, **kwargs: Any) -> Task | bool:
        try:
            response = TaskQueuePutResponse.model_validate(
                _json_request(
                    self.invoke_url,
                    method="POST",
                    json_body=_call_payload(args, kwargs),
                    token=self._token(),
                    timeout_seconds=self.timeout_seconds,
                )
            )
        except ValidationError as exc:
            raise ClientHandleError("task queue returned an invalid response") from exc
        if not response.task_id:
            return False
        return Task(
            task_id=response.task_id,
            client=TaskClient(
                endpoint=_origin(self.invoke_url),
                token=self._token(),
                timeout_seconds=self.timeout_seconds,
            ),
        )

    def put_many(self, items: Iterable[Any], **shared_kwargs: Any) -> TaskBatch:
        submitted: list[Task] = []
        for index, item in enumerate(items):
            task = self.put(item, **shared_kwargs)
            if not isinstance(task, Task):
                raise ClientHandleError(f"failed to enqueue task queue item {index}")
            submitted.append(task)
        return TaskBatch(tuple(submitted))

    async def async_put(self, *args: Any, **kwargs: Any) -> Task | bool:
        return await asyncio.to_thread(self.put, *args, **kwargs)

    async def async_put_many(self, items: Iterable[Any], **shared_kwargs: Any) -> TaskBatch:
        return await asyncio.to_thread(self.put_many, items, **shared_kwargs)


def handle_from_manifest(
    manifest: ResourceManifest | Mapping[str, JsonValue],
) -> ResourceHandle:
    selected = (
        manifest if isinstance(manifest, ResourceManifest) else ResourceManifest.from_dict(manifest)
    )
    if selected.kind is DeploymentKind.Function:
        return FunctionHandle(selected)
    if selected.kind is DeploymentKind.Endpoint:
        return EndpointHandle(selected)
    if selected.kind is DeploymentKind.Asgi:
        return ASGIHandle(selected)
    if selected.kind is DeploymentKind.TaskQueue:
        return TaskQueueHandle(selected)
    return ResourceHandle(selected)


def _call_payload(
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
    *,
    result_format: str = "",
) -> dict[str, JsonValue]:
    payload: dict[str, JsonValue] = {
        "args": [_json_value(item) for item in args],
        "kwargs": {key: _json_value(value) for key, value in kwargs.items()},
    }
    if result_format:
        payload["result_format"] = result_format
    return payload


def _endpoint_request(
    base_url: str,
    *,
    method: str,
    path: str = "",
    json_body: object | None = None,
    data: bytes | str | None = None,
    headers: dict[str, str] | None = None,
    params: dict[str, object] | list[tuple[str, object]] | None = None,
    token: str | None,
    timeout_seconds: float,
) -> EndpointResponse:
    try:
        response = request_raw(
            base_url,
            method=method,
            path=path,
            json_body=json_body,
            data=data,
            headers=headers,
            params=params,
            token=token,
            timeout_seconds=timeout_seconds,
        )
    except ValueError as exc:
        raise ClientHandleError(str(exc)) from exc
    return EndpointResponse(
        status_code=response.status_code,
        headers=response.headers,
        content=response.content,
        url=response.final_url,
    )


def _json_request(
    url: str,
    *,
    method: str,
    json_body: object | None,
    token: str | None,
    timeout_seconds: float,
) -> dict[str, JsonValue]:
    response = request_raw(
        url,
        method=method,
        json_body=json_body,
        data=None,
        headers={"Accept": "application/json"},
        token=token,
        timeout_seconds=timeout_seconds,
    )
    if not 200 <= response.status_code < 300:
        raw = response.content.decode("utf-8", errors="replace")
        raise http_api_error_from_body(
            response.status_code,
            raw,
            fallback=f"HTTP {response.status_code} from {response.final_url}",
        )
    try:
        decoded = parse_json_object(response.content or b"{}")
    except ValueError as exc:
        raise ClientHandleError(f"invalid JSON response from {url}") from exc
    return decoded


def _json_value(value: Any) -> JsonValue:
    return to_json_value(value)


def _manifest_int(data: Mapping[str, JsonValue], key: str) -> int:
    value = data[key]
    if not isinstance(value, str | int | float):
        raise ValueError(f"manifest field {key!r} must be numeric")
    return int(value)


def _manifest_methods(data: Mapping[str, JsonValue]) -> tuple[str, ...]:
    value = data.get("methods")
    if value is None:
        return ()
    if not isinstance(value, list):
        raise ValueError("manifest field 'methods' must be a list")
    return tuple(str(item) for item in value)


def _manifest_object(data: Mapping[str, JsonValue], key: str) -> dict[str, JsonValue]:
    value = data.get(key)
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError(f"manifest field {key!r} must be an object")
    return value


def _endpoint_method(manifest: ResourceManifest) -> str:
    allowed = {item.upper() for item in manifest.methods}
    return "POST" if "POST" in allowed else next(iter(allowed), "POST")


def _origin(url: str) -> str:
    parsed = urllib.parse.urlparse(url)
    return urllib.parse.urlunparse((parsed.scheme, parsed.netloc, "", "", "", ""))


__all__ = [
    "ASGIHandle",
    "ClientHandleError",
    "EndpointHandle",
    "FunctionHandle",
    "ResourceHandle",
    "ResourceManifest",
    "TaskQueueHandle",
    "handle_from_manifest",
]
