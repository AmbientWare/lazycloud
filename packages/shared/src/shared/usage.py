from __future__ import annotations

import math
from collections.abc import Mapping
from datetime import datetime
from typing import TypeAlias
from uuid import NAMESPACE_URL, uuid4, uuid5

from pydantic import Field, JsonValue, SecretStr, TypeAdapter, model_validator

from shared.app_identity import METRICS_NAMESPACE, METRICS_SOURCE
from shared.contracts import ContractModel
from shared.enums import StringEnum
from shared.timestamps import utc_now

METERING_WINDOW_STARTED_AT_METADATA_KEY = "metering_window_started_at"
METERING_WINDOW_ENDED_AT_METADATA_KEY = "metering_window_ended_at"
METERING_OBSERVATION_QUALITY_METADATA_KEY = "metering_observation_quality"
METERING_OBSERVATION_ERROR_TYPE_METADATA_KEY = "metering_observation_error_type"

UsageRecordIdentityPart: TypeAlias = str | int
UsageMetricHeaderValue: TypeAlias = str | SecretStr

_JSON_OBJECT = TypeAdapter(dict[str, JsonValue])


class UsageMetric(StringEnum):
    SchedulerContainerRequested = "container_requested_count"
    SchedulerContainerScheduled = "container_scheduled_count"
    CpuSeconds = "cpu_seconds"
    MemoryGibSeconds = "memory_gib_seconds"
    GpuSeconds = "gpu_seconds"
    TaskCount = "task_count"
    StorageBytes = "storage_bytes"
    PersistentVolumeByteSeconds = "persistent_volume_byte_seconds"
    ContainerDurationMilliseconds = "container_duration_milliseconds"
    ContainerCostCents = "container_cost_cents"
    CpuUsedCoreSeconds = "cpu_used_core_seconds"
    MemoryRssByteSeconds = "memory_rss_byte_seconds"
    MemorySwapByteSeconds = "memory_swap_byte_seconds"
    GpuMemoryByteSeconds = "gpu_memory_byte_seconds"
    NetworkIngressBytes = "network_ingress_bytes"
    NetworkEgressBytes = "network_egress_bytes"
    NetworkIngressPackets = "network_ingress_packets"
    NetworkEgressPackets = "network_egress_packets"
    DiskReadBytes = "disk_read_bytes"
    DiskWriteBytes = "disk_write_bytes"
    ManagedComputeReservationSeconds = "managed_compute_reservation_seconds"
    ManagedComputeReservationCostCents = "managed_compute_reservation_cost_cents"
    CustomerCloudManagementSeconds = "customer_cloud_management_seconds"
    CustomerCloudManagementCostCents = "customer_cloud_management_cost_cents"
    CustomerCloudAllocatedCpuSeconds = "customer_cloud_allocated_cpu_seconds"
    CustomerCloudAllocatedMemoryGibSeconds = "customer_cloud_allocated_memory_gib_seconds"
    CustomerCloudAllocatedGpuSeconds = "customer_cloud_allocated_gpu_seconds"
    CustomerCloudAllocatedDiskGibSeconds = "customer_cloud_allocated_disk_gib_seconds"
    CustomerCloudNetworkIngressBytes = "customer_cloud_network_ingress_bytes"
    CustomerCloudNetworkEgressBytes = "customer_cloud_network_egress_bytes"
    NodeUsage = "node_usage"


class UsageUnit(StringEnum):
    Seconds = "seconds"
    Count = "count"
    Bytes = "bytes"
    GibSeconds = "gib_seconds"
    Milliseconds = "milliseconds"
    Cents = "cents"
    ByteSeconds = "byte_seconds"


class UsageGroupKey(StringEnum):
    """Label keys the usage summary endpoint can group aggregations by."""

    App = "app_id"
    Workload = "stub_id"
    Version = "deployment_id"
    Gpu = "gpu"


class UsageBillingOwner(StringEnum):
    ContainerAllocation = "container_allocation"
    ManagedReservation = "managed_reservation"
    CustomerCloud = "customer_cloud"


class UsageCollectorKind(StringEnum):
    Disabled = "none"
    Prometheus = "prometheus"
    OpenMeter = "openmeter"


class UsageMetricOperation(StringEnum):
    IncrementCounter = "increment-counter"
    SetGauge = "set-gauge"


class MeteringObservationQuality(StringEnum):
    Authoritative = "authoritative"
    CheckpointEstimate = "checkpoint_estimate"


class UsageRecord(ContractModel):
    id: str
    workspace_id: str
    resource_type: str
    resource_id: str
    metric: UsageMetric
    quantity: float
    unit: UsageUnit
    labels: dict[str, str] = Field(default_factory=dict)
    metadata: dict[str, JsonValue] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def validate_container_duration_allocation(self) -> UsageRecord:
        if not math.isfinite(self.quantity) or self.quantity < 0:
            raise ValueError("usage quantity must be finite and nonnegative")
        if self.metric is UsageMetric.PersistentVolumeByteSeconds:
            if self.unit is not UsageUnit.ByteSeconds:
                raise ValueError("persistent volume usage must use byte-seconds")
            return self
        if self.metric is not UsageMetric.ContainerDurationMilliseconds:
            return self
        if self.quantity == 0:
            raise ValueError("container duration usage must be positive")
        if self.unit is not UsageUnit.Milliseconds:
            raise ValueError("container duration usage must use milliseconds")
        allocations: list[float] = []
        for key in ("cpu_millicores", "mem_mb", "gpu_count"):
            raw_value = self.labels.get(key)
            if raw_value is None:
                raise ValueError(f"container duration usage requires the {key} label")
            try:
                value = float(raw_value)
            except ValueError as exc:
                raise ValueError(f"container duration usage {key} must be numeric") from exc
            if not math.isfinite(value):
                raise ValueError(f"container duration usage {key} must be finite")
            if value < 0:
                raise ValueError(f"container duration usage {key} cannot be negative")
            allocations.append(value)
        if not any(value > 0 for value in allocations):
            raise ValueError("container duration usage requires a positive resource allocation")
        return self


def usage_record_id(*parts: UsageRecordIdentityPart) -> str:
    normalized = "|".join(str(part) for part in parts)
    return str(uuid5(NAMESPACE_URL, f"{METRICS_SOURCE}:usage:{normalized}"))


class UsageAggregation(ContractModel):
    workspace_id: str
    metric: UsageMetric
    quantity: float
    unit: UsageUnit
    labels: dict[str, str] = Field(default_factory=dict)


class OpenMeterEvent(ContractModel):
    id: str = Field(default_factory=lambda: str(uuid4()))
    source: str = METRICS_SOURCE
    type: str
    subject: str
    data: dict[str, JsonValue] = Field(default_factory=dict)
    time: datetime


class UsageMetricsSinkSettings(ContractModel):
    collector: UsageCollectorKind = UsageCollectorKind.Disabled
    source: str = METRICS_SOURCE
    prometheus_port: int = 9090
    openmeter_url: str | None = None
    openmeter_api_key: SecretStr | None = None


class UsageMetricEmissionPlan(ContractModel):
    collector: UsageCollectorKind
    operation: UsageMetricOperation
    name: str
    value: float
    source: str
    target: str | None = None
    headers: dict[str, UsageMetricHeaderValue] = Field(default_factory=dict, repr=False)
    metadata: dict[str, JsonValue] = Field(default_factory=dict)
    body: dict[str, JsonValue] = Field(default_factory=dict, repr=False)


def usage_to_prometheus(records: list[UsageRecord]) -> str:
    lines: list[str] = []
    for record in records:
        labels = {
            "workspace": record.workspace_id,
            "resource_type": record.resource_type,
            "resource_id": record.resource_id,
            **record.labels,
        }
        label_text = ",".join(
            f'{key}="{_escape_prometheus_label(value)}"' for key, value in sorted(labels.items())
        )
        lines.append(
            f"{METRICS_NAMESPACE}_usage_{record.metric.value}{{{label_text}}} {record.quantity}"
        )
    return "\n".join(lines) + ("\n" if lines else "")


def usage_to_openmeter_events(
    records: list[UsageRecord],
    *,
    source: str = METRICS_SOURCE,
) -> list[OpenMeterEvent]:
    events: list[OpenMeterEvent] = []
    for record in records:
        labels: dict[str, JsonValue] = {
            str(key): str(value) for key, value in record.labels.items()
        }
        metadata: dict[str, JsonValue] = dict(record.metadata)
        data: dict[str, JsonValue] = {
            "resource_type": record.resource_type,
            "resource_id": record.resource_id,
            "value": record.quantity,
            "quantity": record.quantity,
            "unit": record.unit.value,
            "labels": labels,
            "metadata": metadata,
        }
        events.append(
            OpenMeterEvent(
                id=f"usage-{record.id}",
                source=source,
                type=f"{METRICS_SOURCE}.{record.metric.value}",
                subject=record.workspace_id,
                time=record.created_at,
                data=data,
            )
        )
    return events


def plan_usage_metric_emission(
    settings: UsageMetricsSinkSettings,
    *,
    name: str,
    metadata: Mapping[str, JsonValue] | None = None,
    value: float = 1.0,
    operation: UsageMetricOperation = UsageMetricOperation.IncrementCounter,
) -> UsageMetricEmissionPlan | None:
    normalized = _normalize_metadata(metadata or {})
    if settings.collector == UsageCollectorKind.Disabled:
        return None
    if settings.collector == UsageCollectorKind.Prometheus:
        return UsageMetricEmissionPlan(
            collector=settings.collector,
            operation=operation,
            name=name,
            value=value,
            source=settings.source,
            target=f":{settings.prometheus_port}/metrics",
            metadata=normalized,
            body={
                "metric": name,
                "operation": operation.value,
                "labels": normalized,
                "value": value,
            },
        )
    if settings.collector == UsageCollectorKind.OpenMeter:
        if settings.openmeter_url is None:
            msg = "openmeter_url is required for openmeter usage metrics"
            raise ValueError(msg)
        subject = str(normalized.get("workspace_id") or settings.source)
        headers: dict[str, UsageMetricHeaderValue] = {
            "Content-Type": "application/cloudevents+json"
        }
        if settings.openmeter_api_key is not None:
            api_key = settings.openmeter_api_key.get_secret_value()
            if api_key:
                headers["Authorization"] = SecretStr(f"Bearer {api_key}")
        event = OpenMeterEvent(
            source=settings.source,
            type=name,
            subject=subject,
            time=utc_now(),
            data={**normalized, "value": value},
        )
        return UsageMetricEmissionPlan(
            collector=settings.collector,
            operation=operation,
            name=name,
            value=value,
            source=settings.source,
            target=settings.openmeter_url,
            headers=headers,
            metadata=normalized,
            body=_JSON_OBJECT.validate_json(event.model_dump_json()),
        )
    msg = f"unsupported usage collector: {settings.collector}"
    raise ValueError(msg)


def _normalize_metadata(metadata: Mapping[str, JsonValue]) -> dict[str, JsonValue]:
    return {str(key): value for key, value in sorted(metadata.items())}


def _escape_prometheus_label(value: str) -> str:
    return str(value).replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def usage_metric_from_name(name: str) -> UsageMetric:
    normalized = name.rsplit(".", 1)[-1].replace("-", "_")
    for metric in UsageMetric:
        if metric.value == normalized or name.endswith(metric.value):
            return metric
    return UsageMetric.TaskCount


__all__ = [
    "METERING_OBSERVATION_ERROR_TYPE_METADATA_KEY",
    "METERING_OBSERVATION_QUALITY_METADATA_KEY",
    "METERING_WINDOW_ENDED_AT_METADATA_KEY",
    "METERING_WINDOW_STARTED_AT_METADATA_KEY",
    "MeteringObservationQuality",
    "OpenMeterEvent",
    "UsageAggregation",
    "UsageBillingOwner",
    "UsageCollectorKind",
    "UsageGroupKey",
    "UsageMetric",
    "UsageMetricEmissionPlan",
    "UsageMetricHeaderValue",
    "UsageMetricOperation",
    "UsageMetricsSinkSettings",
    "UsageRecord",
    "UsageRecordIdentityPart",
    "UsageUnit",
    "plan_usage_metric_emission",
    "usage_metric_from_name",
    "usage_record_id",
    "usage_to_openmeter_events",
    "usage_to_prometheus",
]
