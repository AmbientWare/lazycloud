from __future__ import annotations

import math
from datetime import datetime
from typing import TypeAlias
from uuid import NAMESPACE_URL, uuid5

from pydantic import Field, JsonValue, model_validator

from shared.app_identity import METRICS_NAMESPACE, METRICS_SOURCE
from shared.contracts import ContractModel
from shared.enums import StringEnum
from shared.timestamps import utc_now

METERING_WINDOW_STARTED_AT_METADATA_KEY = "metering_window_started_at"
METERING_WINDOW_ENDED_AT_METADATA_KEY = "metering_window_ended_at"
METERING_OBSERVATION_QUALITY_METADATA_KEY = "metering_observation_quality"
METERING_OBSERVATION_ERROR_TYPE_METADATA_KEY = "metering_observation_error_type"

UsageRecordIdentityPart: TypeAlias = str | int


class UsageMetric(StringEnum):
    SchedulerContainerRequested = "container_requested_count"
    SchedulerContainerScheduled = "container_scheduled_count"
    CpuSeconds = "cpu_seconds"
    MemoryGibSeconds = "memory_gib_seconds"
    GpuSeconds = "gpu_seconds"
    TaskCount = "task_count"
    PersistentVolumeByteSeconds = "persistent_volume_byte_seconds"
    ContainerDurationMilliseconds = "container_duration_milliseconds"
    CpuUsedCoreSeconds = "cpu_used_core_seconds"
    MemoryRssByteSeconds = "memory_rss_byte_seconds"
    MemorySwapByteSeconds = "memory_swap_byte_seconds"
    GpuMemoryByteSeconds = "gpu_memory_byte_seconds"
    NetworkIngressBytes = "network_ingress_bytes"
    NetworkEgressBytes = "network_egress_bytes"
    NetworkIngressPackets = "network_ingress_packets"
    NetworkEgressPackets = "network_egress_packets"
    ContainerDiskByteSeconds = "container_disk_byte_seconds"
    DiskReadBytes = "disk_read_bytes"
    DiskWriteBytes = "disk_write_bytes"
    NodeUsage = "node_usage"


class UsageUnit(StringEnum):
    Seconds = "seconds"
    Count = "count"
    Bytes = "bytes"
    GibSeconds = "gib_seconds"
    Milliseconds = "milliseconds"
    ByteSeconds = "byte_seconds"


class UsageGroupKey(StringEnum):
    """Label keys the usage summary endpoint can group aggregations by."""

    App = "app_id"
    Workload = "stub_id"
    Version = "deployment_id"
    Gpu = "gpu"


class UsageBillingOwner(StringEnum):
    """Who pays for the machine a container ran on, which decides how it prices.

    `PlatformFleet` is capacity we buy and resell, and bills at catalog rates.
    `ConnectedCloud` is a customer's own cloud account we provision into: the
    provider already bills them for the machine, so we charge a management fee on
    what we placed rather than the compute itself. `SelfHosted` is hardware
    somebody brought, which we neither buy nor manage and do not bill for.
    """

    PlatformFleet = "platform_fleet"
    ConnectedCloud = "connected_cloud"
    SelfHosted = "self_hosted"


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


def _escape_prometheus_label(value: str) -> str:
    return str(value).replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


__all__ = [
    "METERING_OBSERVATION_ERROR_TYPE_METADATA_KEY",
    "METERING_OBSERVATION_QUALITY_METADATA_KEY",
    "METERING_WINDOW_ENDED_AT_METADATA_KEY",
    "METERING_WINDOW_STARTED_AT_METADATA_KEY",
    "MeteringObservationQuality",
    "UsageAggregation",
    "UsageBillingOwner",
    "UsageGroupKey",
    "UsageMetric",
    "UsageRecord",
    "UsageRecordIdentityPart",
    "UsageUnit",
    "usage_record_id",
    "usage_to_prometheus",
]
