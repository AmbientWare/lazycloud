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
from shared.api import (
    Encoding,
    ErrorCode,
    SubmitTasksRequest,
    TaskInput,
    TaskStatus,
)
from shared.enums import StringEnum
from shared.http.client_manifests import ClientContract
from shared.serialization import to_json_value
from typing_extensions import assert_never

from lazycloud.clients.api import ApiClient, ApiError
from lazycloud.control import api_client, resolve_control_client_config
from lazycloud.exceptions import FunctionNotDeployedError, SdkError
from lazycloud.json_contracts import JsonValue
from lazycloud.session.task import Task


class ClientHandleError(SdkError):
    pass


class ResourceKind(StringEnum):
    Function = "function"


class ResourceManifest(BaseModel):
    """One exported workload as the package and its lock file record it."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    app: str
    name: str
    kind: ResourceKind
    deployment_id: UUID
    deployment_version: int
    release_id: UUID
    client_contract: ClientContract


@dataclass(slots=True)
class FunctionHandle:
    """Submits JSON tasks to a function's active release and waits for their results."""

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
        request = SubmitTasksRequest(
            inputs=[
                TaskInput(
                    encoding=Encoding.json,
                    value={
                        "args": [_json_argument(item) for item in args],
                        "kwargs": {key: _json_argument(item) for key, item in kwargs.items()},
                    },
                )
            ]
        )
        try:
            response = client.submit_tasks(self.workspace, self.app, self.name, request)
        except ApiError as exc:
            if exc.code is ErrorCode.not_found:
                raise FunctionNotDeployedError(self.app, self.name, self.workspace) from exc
            raise
        if len(response.tasks) != 1:
            msg = f"submitting one task returned {len(response.tasks)}"
            raise ClientHandleError(msg)
        task = Task(str(response.tasks[0].id), self.workspace, client)
        view = task.wait()
        if view.status is not TaskStatus.succeeded:
            # Raises the remote exception, a typed failure or the cancellation.
            return task.outcome(view)
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


def handle_from_manifest(
    manifest: ResourceManifest | Mapping[str, JsonValue],
    *,
    endpoint: str,
    workspace: str,
) -> FunctionHandle:
    selected = (
        manifest
        if isinstance(manifest, ResourceManifest)
        else ResourceManifest.model_validate(manifest)
    )
    if selected.kind is ResourceKind.Function:
        return FunctionHandle(selected, endpoint=endpoint, workspace=workspace)
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
    "ClientHandleError",
    "FunctionHandle",
    "ResourceKind",
    "ResourceManifest",
    "handle_from_manifest",
]
