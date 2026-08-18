from __future__ import annotations

from collections.abc import Iterable
from typing import Protocol

from observability.stream_state import RedisStreamRecord, log_record_from_redis
from pydantic import JsonValue
from shared.containers import ContainerRecord
from shared.http.gateway_tasks import EndTaskRequest
from shared.http.objects import ObjectMetadata
from shared.realtime.streams import LogStreamQuery

CONTAINER_OUTPUT_LOG_LIMIT = 100


class GatewayTaskLogEntry(Protocol):
    @property
    def stream(self) -> str: ...

    @property
    def message(self) -> str: ...


class GatewayTaskLogProvider(Protocol):
    def logs(self, task_id: str) -> Iterable[GatewayTaskLogEntry]: ...


class GatewayContainerLogProvider(Protocol):
    def read_logs(
        self,
        query: LogStreamQuery,
        *,
        limit: int | None = None,
    ) -> tuple[RedisStreamRecord, ...]: ...


class GatewayOutputSource(Protocol):
    @property
    def tasks(self) -> GatewayTaskLogProvider: ...


def object_key(metadata: ObjectMetadata, object_hash: str) -> str:
    key = metadata.name or object_hash
    if not key:
        msg = "object name or hash is required"
        raise ValueError(msg)
    return key


def container_output(
    source: GatewayOutputSource,
    container: ContainerRecord,
    *,
    logs: GatewayContainerLogProvider,
) -> str:
    """What the container has written, from whichever store holds it.

    A workload that runs invocations writes through its task, and that record is
    preferred because it is the one attributed to the caller. Everything else —
    a pod, a sandbox, anything with no task at all — is captured by the worker
    and appended to the container's log stream, which is the only place that
    output exists.
    """

    if container.task_id:
        task_output = "\n".join(
            entry.message
            for entry in source.tasks.logs(container.task_id)
            if entry.stream in {"stdout", "stderr"}
        )
        if task_output:
            return task_output

    records = logs.read_logs(
        LogStreamQuery(
            workspace_id=container.workspace_id,
            stub_id=container.stub_id or "",
            container_id=container.id,
            limit=CONTAINER_OUTPUT_LOG_LIMIT,
        ),
        limit=CONTAINER_OUTPUT_LOG_LIMIT,
    )
    decoded = (log_record_from_redis(record) for record in records)
    return "\n".join(
        entry.message for entry in decoded if entry.stream in {"stdout", "stderr"} and entry.message
    )


def task_result_value(request: EndTaskRequest) -> JsonValue:
    raw = request.result_bytes()
    if not raw:
        return None
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return {"value_base64": request.result_base64}


__all__ = [
    "GatewayContainerLogProvider",
    "GatewayOutputSource",
    "GatewayTaskLogEntry",
    "GatewayTaskLogProvider",
    "container_output",
    "object_key",
    "task_result_value",
]
