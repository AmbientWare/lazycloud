from __future__ import annotations

from collections.abc import Iterable
from typing import Protocol

from pydantic import JsonValue
from shared.containers import ContainerRecord
from shared.http.gateway_tasks import EndTaskRequest
from shared.http.objects import ObjectMetadata

CONTAINER_OUTPUT_EVENT_LIMIT = 100


class GatewayTaskLogEntry(Protocol):
    @property
    def stream(self) -> str: ...

    @property
    def message(self) -> str: ...


class GatewayEventRecord(Protocol):
    @property
    def data(self) -> dict[str, JsonValue]: ...


class GatewayTaskLogProvider(Protocol):
    def logs(self, task_id: str) -> Iterable[GatewayTaskLogEntry]: ...


class GatewayEventProvider(Protocol):
    def list_for_resource(
        self,
        *,
        resource_type: str,
        resource_id: str,
        limit: int,
    ) -> Iterable[GatewayEventRecord]: ...


class GatewayOutputSource(Protocol):
    @property
    def tasks(self) -> GatewayTaskLogProvider: ...

    @property
    def events(self) -> GatewayEventProvider: ...


def object_key(metadata: ObjectMetadata, object_hash: str) -> str:
    key = metadata.name or object_hash
    if not key:
        msg = "object name or hash is required"
        raise ValueError(msg)
    return key


def container_output(source: GatewayOutputSource, container: ContainerRecord) -> str:
    if container.task_id:
        task_output = "\n".join(
            entry.message
            for entry in source.tasks.logs(container.task_id)
            if entry.stream in {"stdout", "stderr"}
        )
        if task_output:
            return task_output

    lines: list[str] = []
    for event in source.events.list_for_resource(
        resource_type="container",
        resource_id=container.id,
        limit=CONTAINER_OUTPUT_EVENT_LIMIT,
    ):
        stdout = event.data.get("stdout")
        stderr = event.data.get("stderr")
        if isinstance(stdout, str) and stdout:
            lines.append(stdout)
        if isinstance(stderr, str) and stderr:
            lines.append(stderr)
        if lines:
            break
    return "\n".join(lines)


def task_result_value(request: EndTaskRequest) -> JsonValue:
    raw = request.result_bytes()
    if not raw:
        return None
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return {"value_base64": request.result_base64}


__all__ = [
    "GatewayEventProvider",
    "GatewayEventRecord",
    "GatewayOutputSource",
    "GatewayTaskLogEntry",
    "GatewayTaskLogProvider",
    "container_output",
    "object_key",
    "task_result_value",
]
