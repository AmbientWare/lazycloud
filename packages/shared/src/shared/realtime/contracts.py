from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone
from typing import TypeAlias

from pydantic import Field, JsonValue, TypeAdapter, field_validator

from shared.app_identity import EVENT_SOURCE
from shared.compute_policy import MachinePool
from shared.contracts import ContractModel
from shared.enums import StringEnum
from shared.serialization import to_json_value
from shared.timestamps import utc_now

EVENT_SCHEMA_VERSION = "1.0"
_JSON_OBJECT_ADAPTER: TypeAdapter[dict[str, JsonValue]] = TypeAdapter(dict[str, JsonValue])

EventDataInput: TypeAlias = Mapping[str, JsonValue | datetime] | ContractModel


class EventRecordType(StringEnum):
    TaskUpdated = "task.updated"
    TaskCreated = "task.created"
    ContainerLifecycle = "container.lifecycle"
    ContainerMetrics = "container.metrics"
    ContainerEvent = "container.event"
    ContainerLog = "container.log"
    PlatformLog = "platform.log"
    WorkerLifecycle = "worker.lifecycle"
    StubDeploy = "stub.deploy"
    StubServe = "stub.serve"
    StubRun = "stub.run"
    StubClone = "stub.clone"
    WorkerPoolDegraded = "workerpool.degraded"
    WorkerPoolHealthy = "workerpool.healthy"
    GatewayEndpointCalled = "gateway.endpoint.called"
    ComputeUnit = "compute.unit"
    ComputeJoinToken = "compute.join_token"
    ComputeMachine = "compute.machine"
    ComputeTransport = "compute.transport"
    ComputeRoute = "compute.route"


class EventComputeAction(StringEnum):
    MachineHeartbeat = "machine.heartbeat"


class ContainerMetricsData(ContractModel):
    sample_interval_ms: int = 0
    cpu_used: int = 0
    cpu_total: int = 0
    cpu_pct: float = 0.0
    memory_rss_bytes: int = 0
    memory_vms_bytes: int = 0
    memory_swap_bytes: int = 0
    memory_total_bytes: int = 0
    disk_read_bytes: int = 0
    disk_write_bytes: int = 0
    # Footprint, not throughput: disk_read_bytes/disk_write_bytes above are
    # cumulative /proc io counters and say nothing about space consumed.
    disk_used_bytes: int = 0
    disk_total_bytes: int = 0
    network_recv_bytes: int = 0
    network_sent_bytes: int = 0
    network_recv_packets: int = 0
    network_sent_packets: int = 0
    gpu_memory_used_bytes: int = 0
    gpu_memory_total_bytes: int = 0
    gpu_type: str = ""


class ContainerMetricsPayload(ContractModel):
    worker_id: str
    container_id: str
    workspace_id: str = ""
    stub_id: str = ""
    stub_type: str = ""
    app_id: str = ""
    cpu: int = 0
    gpu_count: int = 0
    metrics: ContainerMetricsData


class EventMetadata(ContractModel):
    container_id: str = ""
    workspace_id: str = ""
    task_id: str = ""
    stub_id: str = ""
    worker_id: str = ""
    machine_id: str = ""
    route_id: str = ""
    service_name: str = ""
    instance_id: str = ""
    app_id: str = ""
    pool: MachinePool = MachinePool("")
    action: str = ""

    @field_validator("*", mode="before")
    @classmethod
    def _string_or_empty(cls, value: object) -> str:
        if value is None:
            return ""
        return str(value).strip()

    def to_extensions(self) -> dict[str, str]:
        extension_keys = {
            "containerid": self.container_id,
            "workspaceid": self.workspace_id,
            "taskid": self.task_id,
            "stubid": self.stub_id,
            "workerid": self.worker_id,
            "machineid": self.machine_id,
            "routeid": self.route_id,
            "servicename": self.service_name,
            "instanceid": self.instance_id,
            "appid": self.app_id,
            "poolname": str(self.pool),
        }
        return {key: value for key, value in extension_keys.items() if value}


class CloudEventRecord(ContractModel):
    specversion: str = EVENT_SCHEMA_VERSION
    id: str
    source: str = EVENT_SOURCE
    type: str
    time: datetime = Field(default_factory=utc_now)
    data: JsonValue = Field(default_factory=dict)
    extensions: dict[str, str] = Field(default_factory=dict)

    def as_envelope(self) -> dict[str, JsonValue]:
        envelope: dict[str, JsonValue] = {
            "specversion": self.specversion,
            "id": self.id,
            "source": self.source,
            "type": self.type,
            "time": self.time.astimezone(timezone.utc).isoformat(),
            "data": self.data,
        }
        envelope.update({key: value for key, value in self.extensions.items() if value})
        return envelope


def create_cloud_event_record(
    event_type: str | EventRecordType,
    data: EventDataInput,
    *,
    event_id: str,
    schema_version: str = EVENT_SCHEMA_VERSION,
    source: str = EVENT_SOURCE,
    now: datetime | None = None,
) -> CloudEventRecord:
    payload = _event_payload(data)
    metadata = event_metadata_from_data(event_type, payload)
    return CloudEventRecord(
        specversion=schema_version,
        id=event_id,
        source=source,
        type=str(event_type),
        time=event_time_for_data(event_type, payload, now=now),
        data=payload,
        extensions=metadata.to_extensions(),
    )


def event_metadata_from_data(
    event_type: str | EventRecordType,
    data: Mapping[str, JsonValue],
) -> EventMetadata:
    event_type_text = str(event_type)
    task_id = _first_text(data, "task_id")
    stub_id = _first_text(data, "stub_id")
    if event_type_text in {EventRecordType.TaskCreated, EventRecordType.TaskUpdated}:
        task_id = task_id or _first_text(data, "id")
    if event_type_text.startswith("stub."):
        stub_id = stub_id or _first_text(data, "id")
    return EventMetadata(
        container_id=_first_text(data, "container_id"),
        workspace_id=_first_text(data, "workspace_id"),
        task_id=task_id,
        stub_id=stub_id,
        worker_id=_first_text(data, "worker_id"),
        machine_id=_first_text(data, "machine_id"),
        route_id=_first_text(data, "route_id"),
        service_name=_first_text(data, "service_name", "service"),
        instance_id=_first_text(data, "instance_id"),
        app_id=_first_text(data, "app_id"),
        pool=MachinePool(_first_text(data, "pool")),
        action=_first_text(data, "action"),
    )


def event_metadata_from_cloud_event(
    event: CloudEventRecord | Mapping[str, JsonValue],
) -> EventMetadata:
    if isinstance(event, CloudEventRecord):
        extensions = event.extensions
    else:
        raw_extensions = event.get("extensions")
        try:
            extensions = _JSON_OBJECT_ADAPTER.validate_python(raw_extensions)
        except ValueError:
            extensions = _JSON_OBJECT_ADAPTER.validate_python(event)
    return EventMetadata(
        container_id=_extension_text(extensions, "containerid"),
        workspace_id=_extension_text(extensions, "workspaceid"),
        task_id=_extension_text(extensions, "taskid"),
        stub_id=_extension_text(extensions, "stubid"),
        worker_id=_extension_text(extensions, "workerid"),
        machine_id=_extension_text(extensions, "machineid"),
        route_id=_extension_text(extensions, "routeid"),
        service_name=_extension_text(extensions, "servicename"),
        instance_id=_extension_text(extensions, "instanceid"),
        app_id=_extension_text(extensions, "appid"),
        pool=MachinePool(_extension_text(extensions, "poolname")),
    )


def event_time_for_data(
    event_type: str | EventRecordType,
    data: Mapping[str, JsonValue],
    *,
    now: datetime | None = None,
) -> datetime:
    event_type_text = str(event_type)
    match event_type_text:
        case EventRecordType.TaskUpdated:
            return (
                _first_datetime(data, "updated_at")
                or _first_datetime(data, "created_at")
                or now
                or utc_now()
            )
        case EventRecordType.TaskCreated:
            return _first_datetime(data, "created_at") or now or utc_now()
        case EventRecordType.ContainerLog | EventRecordType.PlatformLog:
            return _first_datetime(data, "timestamp") or now or utc_now()
        case EventRecordType.ContainerEvent:
            return _first_datetime(data, "timestamp") or now or utc_now()
        case EventRecordType.ContainerLifecycle:
            return _first_datetime(data, "start_time") or now or utc_now()
        case (
            EventRecordType.ComputeUnit
            | EventRecordType.ComputeJoinToken
            | EventRecordType.ComputeMachine
            | EventRecordType.ComputeTransport
            | EventRecordType.ComputeRoute
        ):
            return _first_datetime(data, "timestamp") or now or utc_now()
        case _:
            return now or utc_now()


def _event_payload(data: EventDataInput) -> dict[str, JsonValue]:
    return _JSON_OBJECT_ADAPTER.validate_python(to_json_value(data))


def _first_text(data: Mapping[str, JsonValue], *keys: str) -> str:
    for key in keys:
        value = data.get(key)
        if value is None or isinstance(value, list | dict):
            continue
        text = str(value).strip()
        if text:
            return text
    return ""


def _first_datetime(data: Mapping[str, JsonValue], *keys: str) -> datetime | None:
    for key in keys:
        parsed = _parse_datetime(data.get(key))
        if parsed is not None:
            return parsed
    return None


def _parse_datetime(value: JsonValue) -> datetime | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        seconds = float(value)
        if seconds > 10_000_000_000:
            seconds /= 1000
        current = datetime.fromtimestamp(seconds, timezone.utc)
    elif isinstance(value, str) and value.strip():
        try:
            current = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return current.astimezone(timezone.utc)


def _extension_text(extensions: Mapping[str, JsonValue], key: str) -> str:
    value = extensions.get(key)
    if value is None or isinstance(value, list | dict):
        return ""
    return str(value)
