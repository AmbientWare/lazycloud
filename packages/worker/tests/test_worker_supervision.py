from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import pytest
from api.server.services import ApiServices
from database.repositories.observability import UsageRepository
from pydantic import JsonValue, TypeAdapter, ValidationError
from shared.usage import (
    METERING_WINDOW_ENDED_AT_METADATA_KEY,
    METERING_WINDOW_STARTED_AT_METADATA_KEY,
    UsageMetric,
    UsageRecord,
    UsageUnit,
)
from shared.usage_query import UsageQuery
from shared.worker_events import WorkerEventRecord
from worker.events import ContainerRequestContext, WorkerPoolMode, WorkerUsageEvidence
from worker.execution import WorkerOomWatcherPlan
from worker.runtime_config import OciRuntimeName, OomWatcherKind
from worker.supervision import (
    WORKER_OOM_EVENT_ID,
    WORKER_OOM_EVENT_TYPE,
    WorkerSupervisionService,
)

METERING_WINDOW_STARTED_AT = datetime(2026, 7, 10, 12, 0, tzinfo=UTC)
METERING_WINDOW_ENDED_AT = METERING_WINDOW_STARTED_AT + timedelta(milliseconds=500)

type JsonObject = dict[str, JsonValue]

_JSON_OBJECT: TypeAdapter[JsonObject] = TypeAdapter(JsonObject)


def require_json_object(value: JsonValue | None, *, name: str) -> JsonObject:
    assert isinstance(value, dict), f"{name} must be a JSON object"
    return value


def require_str(value: JsonValue | None, *, name: str) -> str:
    assert isinstance(value, str), f"{name} must be a string"
    return value


def event_payload(record: WorkerEventRecord) -> JsonObject:
    document = _JSON_OBJECT.validate_json(record.model_dump_json())
    return require_json_object(document.get("payload"), name="worker event payload")


@dataclass(slots=True)
class EventSink:
    records: list[WorkerEventRecord] = field(default_factory=list)

    def append(self, record: WorkerEventRecord) -> WorkerEventRecord:
        self.records.append(record)
        return record


@dataclass(slots=True)
class Stopper:
    stopped: list[tuple[str, bool]] = field(default_factory=list)

    def stop_container(self, container_id: str, *, force: bool) -> None:
        self.stopped.append((container_id, force))


@dataclass(slots=True)
class MemoryUsageRecorder:
    records: list[UsageRecord] = field(default_factory=list)

    def record(
        self,
        *,
        id: str | None = None,
        workspace_id: str,
        resource_type: str,
        resource_id: str,
        metric: UsageMetric,
        quantity: float,
        unit: UsageUnit,
        labels: dict[str, str] | None = None,
        metadata: dict[str, JsonValue] | None = None,
    ) -> UsageRecord:
        record = UsageRecord(
            id=id or f"usage-{len(self.records)}",
            workspace_id=workspace_id,
            resource_type=resource_type,
            resource_id=resource_id,
            metric=metric,
            quantity=quantity,
            unit=unit,
            labels=labels or {},
            metadata=metadata or {},
        )
        self.records.append(record)
        return record


def test_worker_supervision_handles_sandbox_oom_with_forced_stop() -> None:
    sink = EventSink()
    stopper = Stopper()
    service = WorkerSupervisionService(
        worker_id="worker-1",
        event_sink=sink,
        container_stopper=stopper,
    )
    request = ContainerRequestContext(
        container_id="ctr-1",
        stub_id="stub-1",
        workspace_id="workspace-1",
        app_id="app-1",
        env=["TASK_ID=task-1"],
        cpu_millicores=2000,
        memory_mib=512,
        gpu_count=1,
    )
    plan = WorkerOomWatcherPlan(
        runtime=OciRuntimeName.Runsc,
        enabled=True,
        watcher=OomWatcherKind.ProcessMemory,
        memory_limit_bytes=512 * 1024 * 1024,
        stop_container_on_oom=True,
        reason="watch sandbox process memory",
    )

    result = service.handle_oom(request, plan)

    assert result.stop_requested
    assert result.stop_invoked
    assert result.stop_error == ""
    assert stopper.stopped == [("ctr-1", True)]
    assert len(sink.records) == 1
    event = sink.records[0]
    assert event.event_type == WORKER_OOM_EVENT_TYPE
    assert event.resource_id == "ctr-1"
    payload = event_payload(event)
    attrs = require_json_object(payload.get("attrs"), name="OOM event attrs")
    assert require_str(payload.get("id"), name="OOM event id") == WORKER_OOM_EVENT_ID
    assert require_str(payload.get("task_id"), name="OOM event task id") == "task-1"
    assert require_str(attrs["oom_killed"], name="OOM event oom_killed") == "true"
    assert (
        require_str(attrs["watcher"], name="OOM event watcher")
        == OomWatcherKind.ProcessMemory.value
    )
    assert require_str(attrs["runtime"], name="OOM event runtime") == OciRuntimeName.Runsc.value


def test_worker_supervision_records_cgroup_oom_without_stop() -> None:
    sink = EventSink()
    stopper = Stopper()
    service = WorkerSupervisionService(
        worker_id="worker-1",
        event_sink=sink,
        container_stopper=stopper,
    )
    request = ContainerRequestContext(container_id="ctr-1", workspace_id="workspace-1")
    plan = WorkerOomWatcherPlan(
        runtime=OciRuntimeName.Runc,
        enabled=True,
        watcher=OomWatcherKind.Cgroup,
        cgroup_path="/sys/fs/cgroup/runtime/ctr-1",
        reason="watch cgroup OOM counter",
    )

    result = service.handle_oom(request, plan)

    assert not result.stop_requested
    assert not result.stop_invoked
    assert stopper.stopped == []
    attrs = require_json_object(event_payload(sink.records[0]).get("attrs"), name="OOM event attrs")
    assert (
        require_str(attrs["cgroup_path"], name="OOM event cgroup path")
        == "/sys/fs/cgroup/runtime/ctr-1"
    )


def test_worker_supervision_records_usage_records(isolated_services: ApiServices) -> None:
    sink = EventSink()
    with isolated_services.context.database.session() as session:
        workspace_id = isolated_services.context.default_workspace_id(session)
        service = WorkerSupervisionService(
            worker_id="worker-1",
            event_sink=sink,
            usage_recorder=UsageRepository(session),
        )
        request = ContainerRequestContext(
            container_id="ctr-1",
            stub_id="stub-1",
            workspace_id=workspace_id,
            app_id="app-1",
            deployment_id="deployment-1",
            cpu_millicores=2000,
            memory_mib=512,
            gpu="L4",
            gpu_count=1,
            cost_per_ms=0.02,
        )

        result = service.record_usage_window(
            request,
            duration_ms=500,
            metering_window_started_at=METERING_WINDOW_STARTED_AT,
            metering_window_ended_at=METERING_WINDOW_ENDED_AT,
            evidence=WorkerUsageEvidence(
                cpu_used_core_seconds=0.25,
                memory_rss_byte_seconds=64 * 1024 * 1024,
                memory_swap_byte_seconds=1024,
                gpu_memory_byte_seconds=32 * 1024 * 1024,
                network_ingress_bytes=100,
                network_egress_bytes=50,
                network_ingress_packets=4,
                network_egress_packets=2,
                disk_read_bytes=4096,
                disk_write_bytes=8192,
            ),
        )
        persisted_records = UsageRepository(session).list(
            UsageQuery(workspace_id=workspace_id),
            workspace_id=workspace_id,
        )

    assert not result.skipped
    assert result.window_start_ms == 0
    assert result.window_end_ms == 500
    assert result.metering_window_started_at == METERING_WINDOW_STARTED_AT
    assert result.metering_window_ended_at == METERING_WINDOW_ENDED_AT
    assert [record.metric for record in result.records] == [
        UsageMetric.ContainerDurationMilliseconds,
        UsageMetric.CpuSeconds,
        UsageMetric.MemoryGibSeconds,
        UsageMetric.GpuSeconds,
        UsageMetric.ContainerCostCents,
        UsageMetric.CpuUsedCoreSeconds,
        UsageMetric.MemoryRssByteSeconds,
        UsageMetric.MemorySwapByteSeconds,
        UsageMetric.GpuMemoryByteSeconds,
        UsageMetric.NetworkIngressBytes,
        UsageMetric.NetworkEgressBytes,
        UsageMetric.NetworkIngressPackets,
        UsageMetric.NetworkEgressPackets,
        UsageMetric.DiskReadBytes,
        UsageMetric.DiskWriteBytes,
    ]
    assert result.records[1].quantity == 1
    assert result.records[2].quantity == 0.25
    assert result.records[3].quantity == 0.5
    assert result.records[0].quantity == 500
    assert result.records[4].quantity == 10
    assert result.records[-4].quantity == 4
    assert result.records[-3].quantity == 2
    assert result.records[-2].quantity == 4096
    assert result.records[-1].quantity == 8192
    assert result.records[0].labels["worker_id"] == "worker-1"
    assert result.records[0].labels["duration_ms"] == "500"
    assert result.records[0].labels["deployment_id"] == "deployment-1"
    assert result.records[0].labels["pool_mode"] == WorkerPoolMode.Public.value
    assert result.records[0].labels["billing_owner"] == "container_allocation"
    assert result.records[0].metadata["window_start_ms"] == 0
    assert result.records[0].metadata["window_end_ms"] == 500
    expected_window_metadata = {
        METERING_WINDOW_STARTED_AT_METADATA_KEY: METERING_WINDOW_STARTED_AT.isoformat(),
        METERING_WINDOW_ENDED_AT_METADATA_KEY: METERING_WINDOW_ENDED_AT.isoformat(),
    }
    assert all(
        {
            key: record.metadata[key]
            for key in (
                METERING_WINDOW_STARTED_AT_METADATA_KEY,
                METERING_WINDOW_ENDED_AT_METADATA_KEY,
            )
        }
        == expected_window_metadata
        for record in result.records
    )
    assert all(
        {
            key: record.metadata[key]
            for key in (
                METERING_WINDOW_STARTED_AT_METADATA_KEY,
                METERING_WINDOW_ENDED_AT_METADATA_KEY,
            )
        }
        == expected_window_metadata
        for record in persisted_records
    )
    assert result.records[4].metadata["worker_metric"] == "container_cost_cents"


def test_worker_supervision_usage_windows_are_idempotent(
    isolated_services: ApiServices,
) -> None:
    sink = EventSink()
    with isolated_services.context.database.session() as session:
        workspace_id = isolated_services.context.default_workspace_id(session)
        service = WorkerSupervisionService(
            worker_id="worker-1",
            event_sink=sink,
            usage_recorder=UsageRepository(session),
        )
        request = ContainerRequestContext(
            container_id="ctr-1",
            stub_id="stub-1",
            workspace_id=workspace_id,
            app_id="app-1",
            cpu_millicores=2000,
            memory_mib=512,
            cost_per_ms=0.02,
        )

        first = service.record_usage_window(
            request,
            duration_ms=500,
            window_start_ms=1000,
            window_end_ms=1500,
            metering_window_started_at=METERING_WINDOW_STARTED_AT,
            metering_window_ended_at=METERING_WINDOW_ENDED_AT,
        )
        second = service.record_usage_window(
            request,
            duration_ms=500,
            window_start_ms=1000,
            window_end_ms=1500,
            metering_window_started_at=METERING_WINDOW_STARTED_AT,
            metering_window_ended_at=METERING_WINDOW_ENDED_AT,
        )
        records = UsageRepository(session).list(
            UsageQuery(workspace_id=workspace_id),
            workspace_id=workspace_id,
        )

    assert [record.id for record in second.records] == [record.id for record in first.records]
    assert sorted(record.metric for record in records) == [
        UsageMetric.ContainerCostCents,
        UsageMetric.ContainerDurationMilliseconds,
        UsageMetric.CpuSeconds,
        UsageMetric.MemoryGibSeconds,
    ]
    assert {record.metric: record.quantity for record in records} == {
        UsageMetric.ContainerCostCents: 10,
        UsageMetric.ContainerDurationMilliseconds: 500,
        UsageMetric.CpuSeconds: 1,
        UsageMetric.MemoryGibSeconds: 0.25,
    }


def test_worker_supervision_records_private_pool_usage_for_managed_billing_owner() -> None:
    sink = EventSink()
    recorder = MemoryUsageRecorder()
    service = WorkerSupervisionService(
        worker_id="worker-1",
        event_sink=sink,
        usage_recorder=recorder,
        pool_mode=WorkerPoolMode.Private,
    )
    request = ContainerRequestContext(
        container_id="ctr-1",
        workspace_id="workspace-1",
        cpu_millicores=1000,
        memory_mib=128,
    )

    result = service.record_usage_window(
        request,
        duration_ms=500,
        metering_window_started_at=METERING_WINDOW_STARTED_AT,
        metering_window_ended_at=METERING_WINDOW_ENDED_AT,
    )

    assert not result.skipped
    assert result.records == recorder.records
    assert {record.metric for record in result.records} == {
        UsageMetric.ContainerDurationMilliseconds,
        UsageMetric.CpuSeconds,
        UsageMetric.MemoryGibSeconds,
    }
    assert all(
        record.labels["pool_mode"] == WorkerPoolMode.Private.value for record in result.records
    )
    assert all(record.labels["billing_owner"] == "managed_reservation" for record in result.records)


def test_worker_supervision_requires_ordered_timezone_aware_metering_window() -> None:
    service = WorkerSupervisionService(
        worker_id="worker-1",
        event_sink=EventSink(),
        usage_recorder=MemoryUsageRecorder(),
    )
    request = ContainerRequestContext(
        container_id="ctr-1",
        workspace_id="workspace-1",
        cpu_millicores=1000,
    )

    with pytest.raises(ValueError, match="start must include a timezone"):
        service.record_usage_window(
            request,
            duration_ms=500,
            metering_window_started_at=METERING_WINDOW_STARTED_AT.replace(tzinfo=None),
            metering_window_ended_at=METERING_WINDOW_ENDED_AT,
        )
    with pytest.raises(ValueError, match="end must be after start"):
        service.record_usage_window(
            request,
            duration_ms=500,
            metering_window_started_at=METERING_WINDOW_STARTED_AT,
            metering_window_ended_at=METERING_WINDOW_STARTED_AT,
        )


@pytest.mark.parametrize(
    ("unit", "labels"),
    [
        (UsageUnit.Seconds, {"cpu_millicores": "1000", "mem_mb": "0", "gpu_count": "0"}),
        (UsageUnit.Milliseconds, {"cpu_millicores": "1000", "mem_mb": "0"}),
        (
            UsageUnit.Milliseconds,
            {"cpu_millicores": "unknown", "mem_mb": "0", "gpu_count": "0"},
        ),
        (UsageUnit.Milliseconds, {"cpu_millicores": "-1", "mem_mb": "0", "gpu_count": "0"}),
        (UsageUnit.Milliseconds, {"cpu_millicores": "0", "mem_mb": "0", "gpu_count": "0"}),
    ],
)
def test_container_duration_usage_requires_complete_allocation_evidence(
    unit: UsageUnit,
    labels: dict[str, str],
) -> None:
    with pytest.raises(ValidationError):
        UsageRecord(
            id="usage-1",
            workspace_id="workspace-1",
            resource_type="container",
            resource_id="container-1",
            metric=UsageMetric.ContainerDurationMilliseconds,
            quantity=1_000,
            unit=unit,
            labels=labels,
        )
