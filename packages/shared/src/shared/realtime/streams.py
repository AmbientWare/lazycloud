from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timezone

from pydantic import Field, JsonValue, field_validator, model_validator

from shared.contracts import ContractModel
from shared.realtime.contracts import (
    CloudEventRecord,
    EventComputeAction,
    EventMetadata,
    EventRecordType,
    event_metadata_from_cloud_event,
)

DEFAULT_EVENT_STREAM_PREFIX = "events"
DEFAULT_EVENT_READ_LIMIT = 10_000
EVENT_HISTORY_READ_LIMIT = 1_000
EVENT_AGGREGATE_SCAN_LIMIT = 50_000
DEFAULT_LOG_READ_LIMIT = 100
MAX_LOG_READ_LIMIT = 1_000
LOG_PAGE_SCAN_LIMIT = 50_000
STUB_SCOPED_CONTAINER_PREFIXES = frozenset({"sandbox", "pod", "endpoint"})


def extract_stub_id_from_stub_scoped_container_id(container_id: str) -> str:
    prefix, separator, _ = container_id.partition("-")
    if not separator or prefix not in STUB_SCOPED_CONTAINER_PREFIXES:
        return ""
    parts = container_id.split("-")
    if len(parts) < 7:
        return ""
    return "-".join(parts[1:6])


class EventAppendRecordPlan(ContractModel):
    body: dict[str, JsonValue]
    streams: tuple[str, ...]
    headers: dict[str, str]
    timestamp_ms: int


class EventHistoryQuery(ContractModel):
    workspace_id: str = ""
    stub_id: str = ""
    container_id: str = ""
    task_id: str = ""
    app_id: str = ""
    event_types: tuple[str, ...] = ()
    exclude_event_types: tuple[str, ...] = ()
    seq_num: int | None = None
    timestamp_ms: int | None = None
    until_ms: int | None = None
    start_time: datetime | None = None
    end_time: datetime | None = None
    limit: int = DEFAULT_EVENT_READ_LIMIT

    @field_validator("limit")
    @classmethod
    def _positive_limit(cls, value: int) -> int:
        if value <= 0:
            return DEFAULT_EVENT_READ_LIMIT
        return value


class LogStreamQuery(ContractModel):
    workspace_id: str
    object_id: str = ""
    object_type: str = ""
    stub_id: str = ""
    container_id: str = ""
    task_id: str = ""
    app_id: str = ""
    deployment_id: str = ""
    machine_id: str = ""
    worker_id: str = ""
    query: str = ""
    limit: int = DEFAULT_LOG_READ_LIMIT
    start_time: datetime | None = None
    end_time: datetime | None = None
    cursor: str = ""
    seq_num: int | None = None
    wait_seconds: float | None = None
    clamp: bool | None = None

    @field_validator(
        "workspace_id",
        "object_id",
        "object_type",
        "stub_id",
        "container_id",
        "task_id",
        "app_id",
        "deployment_id",
        "machine_id",
        "worker_id",
        "query",
        "cursor",
        mode="before",
    )
    @classmethod
    def _text_or_empty(cls, value: object) -> str:
        if value is None:
            return ""
        return str(value).strip()

    @field_validator("limit")
    @classmethod
    def _bounded_limit(cls, value: int) -> int:
        if value <= 0:
            return DEFAULT_LOG_READ_LIMIT
        return min(value, MAX_LOG_READ_LIMIT)

    @field_validator("seq_num")
    @classmethod
    def _non_negative_seq_num(cls, value: int | None) -> int | None:
        if value is None:
            return None
        return max(value, 0)

    @field_validator("wait_seconds")
    @classmethod
    def _non_negative_wait(cls, value: float | None) -> float | None:
        if value is None:
            return None
        return max(value, 0.0)

    @model_validator(mode="after")
    def _apply_object_scope(self) -> LogStreamQuery:
        if self.object_id and self.object_type:
            match self.object_type.strip().lower().replace("-", "_"):
                case "task":
                    object.__setattr__(self, "task_id", self.task_id or self.object_id)
                case "stub":
                    object.__setattr__(self, "stub_id", self.stub_id or self.object_id)
                case "container":
                    object.__setattr__(
                        self,
                        "container_id",
                        self.container_id or self.object_id,
                    )
                case "app":
                    object.__setattr__(self, "app_id", self.app_id or self.object_id)
                case "deployment":
                    object.__setattr__(
                        self,
                        "deployment_id",
                        self.deployment_id or self.object_id,
                    )
                case "machine":
                    object.__setattr__(self, "machine_id", self.machine_id or self.object_id)
                case "workspace":
                    object.__setattr__(self, "workspace_id", self.object_id)
        if not self.object_id:
            object.__setattr__(
                self,
                "object_id",
                (
                    self.container_id
                    or self.task_id
                    or self.stub_id
                    or self.deployment_id
                    or self.app_id
                    or self.machine_id
                    or self.worker_id
                    or self.workspace_id
                ),
            )
        return self


class EventHistoryReadPlan(ContractModel):
    query: EventHistoryQuery
    initial_streams: tuple[str, ...]
    fallback_streams: tuple[str, ...] = ()
    read_from_tail: bool
    read_limit: int
    scan_limit: int = EVENT_AGGREGATE_SCAN_LIMIT
    chunk_size: int = EVENT_HISTORY_READ_LIMIT


class LogPagePlan(ContractModel):
    query: LogStreamQuery
    streams: tuple[str, ...]
    fallback_streams: tuple[str, ...] = ()
    limit: int
    scan_limit: int = LOG_PAGE_SCAN_LIMIT
    chunk_size: int
    next_cursor: str | None = None


class EventSequencedRecord(ContractModel):
    seq_num: int = 0
    timestamp_ms: int | None = None
    headers: dict[str, str] = Field(default_factory=dict)
    body: dict[str, JsonValue] = Field(default_factory=dict)


@dataclass(frozen=True)
class EventStreamPlanner:
    stream_prefix: str = DEFAULT_EVENT_STREAM_PREFIX

    def stream_name_for_event(
        self,
        event_type: str | EventRecordType,
        metadata: EventMetadata,
    ) -> str:
        event_type_text = str(event_type)
        if event_type_text == EventRecordType.ContainerLog:
            return self.primary_log_stream_name(metadata)
        if event_type_text == EventRecordType.PlatformLog:
            return self.platform_log_stream_name(metadata)
        if _is_task_event(event_type_text) and metadata.task_id:
            return self.task_event_stream_name(metadata)
        if metadata.container_id and metadata.workspace_id and metadata.stub_id:
            return self.container_stream_name(
                metadata.workspace_id,
                metadata.stub_id,
                metadata.container_id,
            )
        if event_type_text.startswith("container.") and metadata.container_id:
            return ""
        if metadata.task_id:
            return self.task_event_stream_name(metadata)
        if _is_compute_event(event_type_text) and metadata.workspace_id:
            return self.workspace_stream_name(metadata.workspace_id)
        if metadata.worker_id:
            return self.worker_stream_name(metadata.worker_id)
        if metadata.workspace_id and metadata.stub_id:
            return self.stub_stream_name(metadata.workspace_id, metadata.stub_id)
        if metadata.workspace_id:
            return self.workspace_stream_name(metadata.workspace_id)
        if metadata.pool:
            return self.worker_pool_stream_name(metadata.pool)
        return self.type_stream_name(event_type_text)

    def stream_names_for_event(
        self,
        event_type: str | EventRecordType,
        metadata: EventMetadata,
    ) -> tuple[str, ...]:
        event_type_text = str(event_type)
        if event_type_text == EventRecordType.ContainerLog:
            return self.log_stream_names_for_event(metadata)
        if event_type_text == EventRecordType.PlatformLog:
            stream = self.platform_log_stream_name(metadata)
            streams = [stream] if stream else []
            if metadata.workspace_id:
                streams.append(self.workspace_log_stream_name(metadata.workspace_id))
            return _unique_streams(streams)
        streams: list[str] = []
        _add_unique(streams, self.stream_name_for_event(event_type_text, metadata))
        if should_write_container_alias(metadata):
            _add_unique(
                streams,
                self.container_alias_stream_name(
                    metadata.workspace_id,
                    metadata.container_id,
                ),
            )
        if (
            _is_task_event(event_type_text)
            and metadata.container_id
            and metadata.workspace_id
            and metadata.stub_id
        ):
            _add_unique(
                streams,
                self.container_stream_name(
                    metadata.workspace_id,
                    metadata.stub_id,
                    metadata.container_id,
                ),
            )
        if metadata.workspace_id and metadata.stub_id:
            _add_unique(streams, self.stub_stream_name(metadata.workspace_id, metadata.stub_id))
        if _is_stub_event(event_type_text) and metadata.workspace_id:
            _add_unique(streams, self.workspace_stream_name(metadata.workspace_id))
        if _is_workspace_container_realtime_event(event_type_text) and metadata.workspace_id:
            _add_unique(streams, self.workspace_stream_name(metadata.workspace_id))
            if metadata.app_id:
                _add_unique(
                    streams,
                    self.app_namespace_stream_name(metadata.workspace_id, metadata.app_id),
                )
        if _is_compute_event(event_type_text) and metadata.workspace_id:
            _add_unique(streams, self.workspace_stream_name(metadata.workspace_id))
            if metadata.action != EventComputeAction.MachineHeartbeat:
                _add_unique(
                    streams,
                    self.workspace_compute_stream_name(metadata.workspace_id),
                )
        if _is_task_event(event_type_text) and metadata.workspace_id:
            _add_unique(streams, self.workspace_stream_name(metadata.workspace_id))
            if metadata.app_id:
                _add_unique(
                    streams,
                    self.app_namespace_stream_name(metadata.workspace_id, metadata.app_id),
                )
        return tuple(streams)

    def append_record_for_event(self, event: CloudEventRecord) -> EventAppendRecordPlan:
        metadata = event_metadata_from_cloud_event(event)
        return EventAppendRecordPlan(
            body=event.as_envelope(),
            streams=self.stream_names_for_event(event.type, metadata),
            headers=event_headers(event, metadata),
            timestamp_ms=datetime_to_millis(event.time),
        )

    def resolve_container_streams(
        self,
        container_id: str,
        query: EventHistoryQuery,
    ) -> tuple[str, ...]:
        if query.workspace_id and query.stub_id:
            return (
                self.container_stream_name(
                    query.workspace_id,
                    query.stub_id,
                    container_id,
                ),
            )
        if not query.workspace_id:
            return ()
        stub_id = extract_stub_id_from_stub_scoped_container_id(container_id)
        if stub_id:
            return (self.container_stream_name(query.workspace_id, stub_id, container_id),)
        return (self.container_alias_stream_name(query.workspace_id, container_id),)

    def resolve_event_history_streams(self, query: EventHistoryQuery) -> tuple[str, ...]:
        if query.container_id and query.workspace_id:
            return self.resolve_container_streams(query.container_id, query)
        if query.task_id and query.workspace_id and query.stub_id:
            return (self.stub_task_stream_name(query.workspace_id, query.stub_id),)
        if query.app_id and query.workspace_id:
            return (self.app_namespace_stream_name(query.workspace_id, query.app_id),)
        if query.stub_id and query.workspace_id:
            return (self.stub_stream_name(query.workspace_id, query.stub_id),)
        if query.workspace_id and all_compute_event_types(query.event_types):
            return (self.workspace_compute_stream_name(query.workspace_id),)
        if query.workspace_id:
            return (self.workspace_stream_name(query.workspace_id),)
        return ()

    def event_history_fallback_streams(
        self,
        query: EventHistoryQuery,
        *,
        events_found: int,
        streams_read: Iterable[str] = (),
    ) -> tuple[str, ...]:
        if events_found:
            return ()
        read = set(streams_read)
        fallbacks: list[str] = []
        if query.workspace_id and all_compute_event_types(query.event_types):
            _add_unread(fallbacks, read, self.workspace_stream_name(query.workspace_id))
        if (
            query.app_id
            and query.workspace_id
            and all_workspace_container_realtime_event_types(query.event_types)
        ):
            _add_unread(fallbacks, read, self.workspace_stream_name(query.workspace_id))
        return tuple(fallbacks)

    def plan_event_history_read(
        self,
        query: EventHistoryQuery,
        *,
        events_found: int = 0,
        streams_read: Iterable[str] = (),
    ) -> EventHistoryReadPlan:
        planned_query = query
        if query.task_id and not query.event_types:
            planned_query = query.model_copy(
                update={
                    "exclude_event_types": _unique_streams(
                        [*query.exclude_event_types, EventRecordType.ContainerLog]
                    )
                }
            )
        initial_streams = self.resolve_event_history_streams(planned_query)
        return EventHistoryReadPlan(
            query=planned_query,
            initial_streams=initial_streams,
            fallback_streams=self.event_history_fallback_streams(
                planned_query,
                events_found=events_found,
                streams_read=(*streams_read, *initial_streams),
            ),
            read_from_tail=event_history_query_reads_from_tail(planned_query),
            read_limit=planned_query.limit,
        )

    def resolve_log_streams(self, query: LogStreamQuery) -> tuple[str, ...]:
        if query.machine_id:
            return self.machine_log_streams(query)
        if query.task_id and query.workspace_id and query.stub_id:
            return (self.stub_task_stream_name(query.workspace_id, query.stub_id),)
        if query.task_id and query.workspace_id:
            return (self.workspace_log_stream_name(query.workspace_id),)
        if query.container_id and query.workspace_id and query.stub_id:
            return (
                self.container_log_stream_name(
                    query.workspace_id,
                    query.stub_id,
                    query.container_id,
                ),
            )
        if query.container_id and query.workspace_id:
            stub_id = extract_stub_id_from_stub_scoped_container_id(query.container_id)
            if stub_id:
                return (
                    self.container_log_stream_name(
                        query.workspace_id,
                        stub_id,
                        query.container_id,
                    ),
                )
            return (
                self.container_log_alias_stream_name(
                    query.workspace_id,
                    query.container_id,
                ),
            )
        if query.stub_id and query.workspace_id:
            return (self.stub_log_stream_name(query.workspace_id, query.stub_id),)
        if query.app_id and query.workspace_id:
            return (self.app_namespace_log_stream_name(query.workspace_id, query.app_id),)
        if query.workspace_id:
            return (self.workspace_log_stream_name(query.workspace_id),)
        return ()

    def machine_log_streams(self, query: LogStreamQuery) -> tuple[str, ...]:
        if query.workspace_id:
            return (self.workspace_log_stream_name(query.workspace_id),)
        return _unique_streams(
            [
                self.platform_log_stream_name(
                    EventMetadata(service_name="agent", instance_id=query.machine_id)
                ),
                self.platform_log_stream_name(EventMetadata(worker_id=query.worker_id)),
            ]
        )

    def plan_log_page(
        self,
        query: LogStreamQuery,
    ) -> LogPagePlan:
        streams = self.resolve_log_streams(query)
        return LogPagePlan(
            query=query,
            streams=streams,
            fallback_streams=(),
            limit=query.limit,
            chunk_size=min(max(query.limit, DEFAULT_LOG_READ_LIMIT), MAX_LOG_READ_LIMIT),
        )

    def container_stream_name(self, workspace_id: str, stub_id: str, container_id: str) -> str:
        return (
            f"{self.stream_prefix}/workspaces/{event_stream_part(workspace_id)}"
            f"/stubs/{event_stream_part(stub_id)}"
            f"/containers/{event_stream_part(container_id)}"
        )

    def container_alias_stream_name(self, workspace_id: str, container_id: str) -> str:
        return (
            f"{self.stream_prefix}/workspaces/{event_stream_part(workspace_id)}"
            f"/containers/{event_stream_part(container_id)}"
        )

    def stub_task_stream_name(self, workspace_id: str, stub_id: str) -> str:
        return (
            f"{self.stream_prefix}/workspaces/{event_stream_part(workspace_id)}"
            f"/stubs/{event_stream_part(stub_id)}/tasks"
        )

    def task_event_stream_name(self, metadata: EventMetadata) -> str:
        if metadata.workspace_id and metadata.stub_id:
            return self.stub_task_stream_name(metadata.workspace_id, metadata.stub_id)
        if metadata.workspace_id:
            return self.workspace_stream_name(metadata.workspace_id)
        return ""

    def worker_stream_name(self, worker_id: str) -> str:
        return f"{self.stream_prefix}/workers/{event_stream_part(worker_id)}"

    def worker_pool_stream_name(self, pool: str) -> str:
        return f"{self.stream_prefix}/worker-pools/{event_stream_part(pool)}"

    def workspace_stream_name(self, workspace_id: str) -> str:
        return f"{self.stream_prefix}/workspaces/{event_stream_part(workspace_id)}"

    def workspace_compute_stream_name(self, workspace_id: str) -> str:
        return f"{self.workspace_stream_name(workspace_id)}/compute"

    def stub_stream_name(self, workspace_id: str, stub_id: str) -> str:
        return f"{self.workspace_stream_name(workspace_id)}/stubs/{event_stream_part(stub_id)}"

    def app_namespace_stream_name(self, workspace_id: str, app_id: str) -> str:
        return f"{self.workspace_stream_name(workspace_id)}/apps/{event_stream_part(app_id)}"

    def primary_log_stream_name(self, metadata: EventMetadata) -> str:
        if metadata.container_id and metadata.workspace_id and metadata.stub_id:
            return self.container_log_stream_name(
                metadata.workspace_id,
                metadata.stub_id,
                metadata.container_id,
            )
        if metadata.task_id and metadata.workspace_id and metadata.stub_id:
            return self.stub_task_stream_name(metadata.workspace_id, metadata.stub_id)
        if metadata.task_id and metadata.workspace_id:
            return self.workspace_log_stream_name(metadata.workspace_id)
        if metadata.stub_id and metadata.workspace_id:
            return self.stub_log_stream_name(metadata.workspace_id, metadata.stub_id)
        if metadata.app_id and metadata.workspace_id:
            return self.app_namespace_log_stream_name(metadata.workspace_id, metadata.app_id)
        if metadata.workspace_id:
            return self.workspace_log_stream_name(metadata.workspace_id)
        return ""

    def log_stream_names_for_event(self, metadata: EventMetadata) -> tuple[str, ...]:
        if not metadata.workspace_id:
            return ()
        streams: list[str] = []
        if metadata.container_id and metadata.stub_id:
            _add_unique(
                streams,
                self.container_log_stream_name(
                    metadata.workspace_id,
                    metadata.stub_id,
                    metadata.container_id,
                ),
            )
        if should_write_container_alias(metadata):
            _add_unique(
                streams,
                self.container_log_alias_stream_name(
                    metadata.workspace_id,
                    metadata.container_id,
                ),
            )
        if metadata.stub_id:
            _add_unique(streams, self.stub_log_stream_name(metadata.workspace_id, metadata.stub_id))
        if metadata.task_id and metadata.stub_id:
            _add_unique(
                streams,
                self.stub_task_stream_name(metadata.workspace_id, metadata.stub_id),
            )
        if metadata.app_id:
            _add_unique(
                streams,
                self.app_namespace_log_stream_name(metadata.workspace_id, metadata.app_id),
            )
        _add_unique(streams, self.workspace_log_stream_name(metadata.workspace_id))
        return tuple(streams)

    def container_log_stream_name(self, workspace_id: str, stub_id: str, container_id: str) -> str:
        return (
            f"{self.stream_prefix}/logs/workspaces/{event_stream_part(workspace_id)}"
            f"/stubs/{event_stream_part(stub_id)}"
            f"/containers/{event_stream_part(container_id)}"
        )

    def container_log_alias_stream_name(self, workspace_id: str, container_id: str) -> str:
        return (
            f"{self.stream_prefix}/logs/workspaces/{event_stream_part(workspace_id)}"
            f"/containers/{event_stream_part(container_id)}"
        )

    def stub_log_stream_name(self, workspace_id: str, stub_id: str) -> str:
        return (
            f"{self.stream_prefix}/logs/workspaces/{event_stream_part(workspace_id)}"
            f"/stubs/{event_stream_part(stub_id)}"
        )

    def app_namespace_log_stream_name(self, workspace_id: str, app_id: str) -> str:
        return (
            f"{self.stream_prefix}/logs/workspaces/{event_stream_part(workspace_id)}"
            f"/apps/{event_stream_part(app_id)}"
        )

    def workspace_log_stream_name(self, workspace_id: str) -> str:
        return f"{self.stream_prefix}/logs/workspaces/{event_stream_part(workspace_id)}"

    def platform_log_stream_name(self, metadata: EventMetadata) -> str:
        if metadata.worker_id:
            return (
                f"{self.stream_prefix}/logs/platform/workers/"
                f"{event_stream_part(metadata.worker_id)}"
            )
        if metadata.service_name and metadata.instance_id:
            return (
                f"{self.stream_prefix}/logs/platform/services/"
                f"{event_stream_part(metadata.service_name)}/"
                f"{event_stream_part(metadata.instance_id)}"
            )
        if metadata.service_name:
            return (
                f"{self.stream_prefix}/logs/platform/services/"
                f"{event_stream_part(metadata.service_name)}"
            )
        return ""

    def type_stream_name(self, event_type: str) -> str:
        return f"{self.stream_prefix}/types/{event_type.replace('.', '-')}"


def event_stream_part(value: str) -> str:
    return value.strip().strip("/").replace("/", "_")


def should_write_container_alias(metadata: EventMetadata) -> bool:
    if not metadata.container_id or not metadata.workspace_id:
        return False
    scoped_stub_id = extract_stub_id_from_stub_scoped_container_id(metadata.container_id)
    return not scoped_stub_id or not metadata.stub_id or metadata.stub_id != scoped_stub_id


def event_headers(
    event: CloudEventRecord,
    metadata: EventMetadata | None = None,
) -> dict[str, str]:
    metadata = metadata or event_metadata_from_cloud_event(event)
    headers = {"type": event.type, "id": event.id}
    metadata_headers = {
        "container_id": metadata.container_id,
        "workspace_id": metadata.workspace_id,
        "task_id": metadata.task_id,
        "stub_id": metadata.stub_id,
        "worker_id": metadata.worker_id,
        "machine_id": metadata.machine_id,
        "route_id": metadata.route_id,
        "service": metadata.service_name,
        "instance_id": metadata.instance_id,
        "app_id": metadata.app_id,
        "pool": str(metadata.pool),
    }
    headers.update({key: value for key, value in metadata_headers.items() if value})
    return headers


def event_query_allows_type(query: EventHistoryQuery, event_type: str) -> bool:
    if _event_type_matches_any(query.exclude_event_types, event_type):
        return False
    if not query.event_types:
        return True
    return _event_type_matches_any(query.event_types, event_type)


def event_record_headers_skip(
    record: EventSequencedRecord,
    query: EventHistoryQuery,
) -> bool:
    event_type = record.headers.get("type")
    if event_type and not event_query_allows_type(query, event_type):
        return True
    for query_value, header_key in (
        (query.task_id, "task_id"),
        (query.container_id, "container_id"),
        (query.stub_id, "stub_id"),
        (query.app_id, "app_id"),
    ):
        header_value = record.headers.get(header_key)
        if query_value and header_value and header_value != query_value:
            return True
    return False


def log_record_headers_skip(record: EventSequencedRecord, query: LogStreamQuery) -> bool:
    event_type = record.headers.get("type")
    if event_type and event_type not in {
        EventRecordType.ContainerLog,
        EventRecordType.PlatformLog,
    }:
        return True
    for query_value, header_key in (
        (query.task_id, "task_id"),
        (query.container_id, "container_id"),
        (query.stub_id, "stub_id"),
        (query.app_id, "app_id"),
        (query.deployment_id, "deployment_id"),
        (query.machine_id, "machine_id"),
        (query.worker_id, "worker_id"),
    ):
        header_value = record.headers.get(header_key)
        if query_value and header_value and header_value != query_value:
            return True
    return False


def event_history_query_reads_from_tail(query: EventHistoryQuery) -> bool:
    return (
        query.seq_num is None
        and query.start_time is None
        and query.timestamp_ms is None
        and query.end_time is None
        and query.until_ms is None
    )


def all_compute_event_types(event_types: Iterable[str]) -> bool:
    values = [value.strip() for value in event_types if value.strip()]
    return bool(values) and all(_is_compute_event(value) for value in values)


def all_workspace_container_realtime_event_types(event_types: Iterable[str]) -> bool:
    allowed = {
        EventRecordType.ContainerMetrics,
        EventRecordType.ContainerEvent,
        EventRecordType.ContainerLifecycle,
    }
    values = [value.strip() for value in event_types if value.strip()]
    return bool(values) and all(value in allowed for value in values)


def datetime_to_millis(value: datetime) -> int:
    return int(value.astimezone(timezone.utc).timestamp() * 1000)


def _is_task_event(event_type: str) -> bool:
    return event_type in {EventRecordType.TaskCreated, EventRecordType.TaskUpdated}


def _event_type_matches_any(patterns: Iterable[str], event_type: str) -> bool:
    for pattern in patterns:
        current = pattern.strip()
        if current == event_type:
            return True
        if current.endswith("*") and event_type.startswith(current.removesuffix("*")):
            return True
    return False


def _is_stub_event(event_type: str) -> bool:
    return event_type.startswith("stub.")


def _is_workspace_container_realtime_event(event_type: str) -> bool:
    return event_type in {
        EventRecordType.ContainerMetrics,
        EventRecordType.ContainerEvent,
        EventRecordType.ContainerLifecycle,
    }


def _is_compute_event(event_type: str) -> bool:
    return event_type.startswith("compute.")


def _add_unique(streams: list[str], stream: str) -> None:
    if stream and stream not in streams:
        streams.append(stream)


def _add_unread(streams: list[str], read: set[str], stream: str) -> None:
    if stream and stream not in read:
        _add_unique(streams, stream)


def _unique_streams(streams: Iterable[str]) -> tuple[str, ...]:
    unique: list[str] = []
    for stream in streams:
        _add_unique(unique, stream)
    return tuple(unique)
