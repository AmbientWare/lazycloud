from __future__ import annotations

import http.client
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol
from urllib.parse import urlparse
from uuid import uuid4

from pydantic import Field, JsonValue, TypeAdapter, ValidationError
from shared.contracts import ContractModel
from shared.usage import (
    METERING_WINDOW_ENDED_AT_METADATA_KEY,
    METERING_WINDOW_STARTED_AT_METADATA_KEY,
    UsageMetric,
    UsageRecord,
    UsageUnit,
    usage_record_id,
)
from shared.worker_events import WorkerEventRecord

from worker.events import (
    ContainerEventPayload,
    ContainerRequestContext,
    WorkerPoolMode,
    WorkerUsageEvidence,
    WorkerUsageMetricName,
    WorkerUsageMetricPlan,
    plan_worker_usage_metrics,
    populate_container_event,
)
from worker.execution import WorkerOomWatcherPlan

WORKER_OOM_EVENT_TYPE = "container.oom_killed"
WORKER_OOM_EVENT_ID = "runtime.oom_killed"
WORKER_OOM_MESSAGE = "container exceeded its memory limit"
WORKER_OOM_EXIT_MESSAGE = "container exited with code 137 due to out-of-memory kill"

type JsonObject = dict[str, JsonValue]

_JSON_OBJECT: TypeAdapter[JsonObject] = TypeAdapter(JsonObject)


class WorkerEventSink(Protocol):
    def append(self, record: WorkerEventRecord) -> WorkerEventRecord: ...


class WorkerContainerStopper(Protocol):
    def stop_container(self, container_id: str, *, force: bool) -> None: ...


class WorkerUsageRecorder(Protocol):
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
    ) -> UsageRecord: ...


class WorkerContainerCostResolver(Protocol):
    def cost_per_ms(self, request: ContainerRequestContext) -> float: ...


@dataclass(slots=True)
class HttpWorkerContainerCostResolver:
    endpoint: str
    token: str
    timeout_seconds: float = 10.0

    def cost_per_ms(self, request: ContainerRequestContext) -> float:
        if not self.endpoint or not self.token:
            return 0.0
        payload: JsonObject = {
            "cpu": request.cpu_millicores,
            "memory": request.memory_mib,
            "gpu": request.gpu,
            "gpu_count": request.gpu_count,
        }
        endpoint = urlparse(self.endpoint)
        if endpoint.scheme not in {"http", "https"} or endpoint.hostname is None:
            raise RuntimeError("container cost hook must be an HTTP(S) URL with a hostname")
        connection: http.client.HTTPConnection
        if endpoint.scheme == "https":
            connection = http.client.HTTPSConnection(
                endpoint.hostname,
                endpoint.port,
                timeout=self.timeout_seconds,
            )
        else:
            connection = http.client.HTTPConnection(
                endpoint.hostname,
                endpoint.port,
                timeout=self.timeout_seconds,
            )
        request_target = endpoint.path or "/"
        if endpoint.query:
            request_target = f"{request_target}?{endpoint.query}"
        try:
            connection.request(
                "POST",
                request_target,
                body=json.dumps(payload, separators=(",", ":")),
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {self.token}",
                },
            )
            response = connection.getresponse()
            body = response.read(1 << 20)
            if response.status < 200 or response.status >= 300:
                detail = body.decode("utf-8", errors="replace")
                raise RuntimeError(
                    f"container cost hook failed with status {response.status}: {detail}"
                )
        except OSError as exc:
            raise RuntimeError(f"container cost hook failed: {exc}") from exc
        finally:
            connection.close()
        try:
            decoded = _JSON_OBJECT.validate_json(body or b"{}")
        except ValidationError as exc:
            raise RuntimeError("container cost hook response must be an object") from exc
        cost_per_ms = decoded.get("cost_per_ms")
        if isinstance(cost_per_ms, bool | int | float | str):
            return float(cost_per_ms or 0.0)
        return 0.0


class WorkerOomHandlingResult(ContractModel):
    container_id: str
    event: WorkerEventRecord
    output_message: str = WORKER_OOM_EXIT_MESSAGE
    stop_requested: bool = False
    stop_invoked: bool = False
    stop_error: str = ""


class WorkerUsageEmissionResult(ContractModel):
    worker_id: str
    container_id: str
    duration_ms: int
    window_start_ms: int = 0
    window_end_ms: int = 0
    metering_window_started_at: datetime
    metering_window_ended_at: datetime
    pool_mode: WorkerPoolMode
    plans: tuple[WorkerUsageMetricPlan, ...] = Field(default_factory=tuple)
    records: list[UsageRecord] = Field(default_factory=list)
    skipped: bool = False
    reason: str = ""


@dataclass(slots=True)
class WorkerSupervisionService:
    worker_id: str
    event_sink: WorkerEventSink
    usage_recorder: WorkerUsageRecorder | None = None
    container_stopper: WorkerContainerStopper | None = None
    cost_resolver: WorkerContainerCostResolver | None = None
    pool_mode: WorkerPoolMode = WorkerPoolMode.Public

    def handle_oom(
        self,
        request: ContainerRequestContext,
        watcher_plan: WorkerOomWatcherPlan,
    ) -> WorkerOomHandlingResult:
        event = self.event_sink.append(
            WorkerEventRecord(
                id=str(uuid4()),
                worker_id=self.worker_id,
                event_type=WORKER_OOM_EVENT_TYPE,
                resource_id=request.container_id,
                payload=self._oom_event_payload(request, watcher_plan),
            )
        )
        stop_error = ""
        stop_invoked = False
        if watcher_plan.stop_container_on_oom:
            if self.container_stopper is None:
                stop_error = "stop container requested but no stopper is configured"
            else:
                try:
                    self.container_stopper.stop_container(request.container_id, force=True)
                    stop_invoked = True
                except Exception as exc:  # pragma: no cover - defensive boundary capture
                    stop_error = f"{type(exc).__name__}: {exc}"
        return WorkerOomHandlingResult(
            container_id=request.container_id,
            event=event,
            stop_requested=watcher_plan.stop_container_on_oom,
            stop_invoked=stop_invoked,
            stop_error=stop_error,
        )

    def record_usage_window(
        self,
        request: ContainerRequestContext,
        *,
        duration_ms: int,
        cost_per_ms: float | None = None,
        window_start_ms: int = 0,
        window_end_ms: int | None = None,
        metering_window_started_at: datetime,
        metering_window_ended_at: datetime,
        evidence: WorkerUsageEvidence | None = None,
    ) -> WorkerUsageEmissionResult:
        window_start_ms, window_end_ms = _usage_window_bounds(
            duration_ms=duration_ms,
            window_start_ms=window_start_ms,
            window_end_ms=window_end_ms,
        )
        metering_window_started_at, metering_window_ended_at = _utc_metering_window(
            started_at=metering_window_started_at,
            ended_at=metering_window_ended_at,
        )
        if self.usage_recorder is None:
            return WorkerUsageEmissionResult(
                worker_id=self.worker_id,
                container_id=request.container_id,
                duration_ms=duration_ms,
                window_start_ms=window_start_ms,
                window_end_ms=window_end_ms,
                metering_window_started_at=metering_window_started_at,
                metering_window_ended_at=metering_window_ended_at,
                pool_mode=self.pool_mode,
                skipped=True,
                reason="usage recorder is not configured",
            )
        if duration_ms <= 0:
            return WorkerUsageEmissionResult(
                worker_id=self.worker_id,
                container_id=request.container_id,
                duration_ms=duration_ms,
                window_start_ms=window_start_ms,
                window_end_ms=window_end_ms,
                metering_window_started_at=metering_window_started_at,
                metering_window_ended_at=metering_window_ended_at,
                pool_mode=self.pool_mode,
                skipped=True,
                reason="usage duration must be positive",
            )
        if not request.workspace_id:
            return WorkerUsageEmissionResult(
                worker_id=self.worker_id,
                container_id=request.container_id,
                duration_ms=duration_ms,
                window_start_ms=window_start_ms,
                window_end_ms=window_end_ms,
                metering_window_started_at=metering_window_started_at,
                metering_window_ended_at=metering_window_ended_at,
                pool_mode=self.pool_mode,
                skipped=True,
                reason="workspace id is required for usage records",
            )

        resolved_cost = self._cost_per_ms(request, cost_per_ms)
        plans = plan_worker_usage_metrics(
            worker_id=self.worker_id,
            request=request,
            duration_ms=duration_ms,
            pool_mode=self.pool_mode,
            cost_per_ms=resolved_cost,
            evidence=evidence,
        )
        if not plans:
            return WorkerUsageEmissionResult(
                worker_id=self.worker_id,
                container_id=request.container_id,
                duration_ms=duration_ms,
                window_start_ms=window_start_ms,
                window_end_ms=window_end_ms,
                metering_window_started_at=metering_window_started_at,
                metering_window_ended_at=metering_window_ended_at,
                pool_mode=self.pool_mode,
                plans=plans,
                skipped=True,
                reason="pool mode does not emit worker usage",
            )

        records = [
            self._record_usage_plan(
                request,
                plan,
                window_start_ms=window_start_ms,
                window_end_ms=window_end_ms,
                metering_window_started_at=metering_window_started_at,
                metering_window_ended_at=metering_window_ended_at,
            )
            for plan in plans
        ]
        return WorkerUsageEmissionResult(
            worker_id=self.worker_id,
            container_id=request.container_id,
            duration_ms=duration_ms,
            window_start_ms=window_start_ms,
            window_end_ms=window_end_ms,
            metering_window_started_at=metering_window_started_at,
            metering_window_ended_at=metering_window_ended_at,
            pool_mode=self.pool_mode,
            plans=plans,
            records=records,
            reason="worker usage emitted",
        )

    def _oom_event_payload(
        self,
        request: ContainerRequestContext,
        watcher_plan: WorkerOomWatcherPlan,
    ) -> JsonObject:
        attrs = {
            "oom_killed": "true",
            "runtime": watcher_plan.runtime.value,
            "watcher": watcher_plan.watcher.value if watcher_plan.watcher is not None else "",
            "memory_limit_bytes": str(watcher_plan.memory_limit_bytes or ""),
            "cgroup_path": watcher_plan.cgroup_path or "",
        }
        payload = populate_container_event(
            ContainerEventPayload(
                id=WORKER_OOM_EVENT_ID,
                reason="OOM",
                source="worker-runtime",
                message=WORKER_OOM_MESSAGE,
                attrs={key: value for key, value in attrs.items() if value},
            ),
            request,
            worker_id=self.worker_id,
        )
        return _JSON_OBJECT.validate_json(payload.model_dump_json())

    def _record_usage_plan(
        self,
        request: ContainerRequestContext,
        plan: WorkerUsageMetricPlan,
        *,
        window_start_ms: int,
        window_end_ms: int,
        metering_window_started_at: datetime,
        metering_window_ended_at: datetime,
    ) -> UsageRecord:
        if self.usage_recorder is None:
            msg = "usage recorder is not configured"
            raise RuntimeError(msg)
        metric, unit = usage_record_kind(plan.name)
        labels = {key: str(value) for key, value in plan.labels.items()}
        return self.usage_recorder.record(
            id=usage_record_id(
                metric.value,
                request.workspace_id,
                request.container_id,
                self.worker_id,
                window_start_ms,
                window_end_ms,
            ),
            workspace_id=request.workspace_id,
            resource_type="container",
            resource_id=request.container_id,
            metric=metric,
            quantity=plan.value,
            unit=unit,
            labels=labels,
            metadata={
                "worker_id": self.worker_id,
                "worker_metric": plan.name.value,
                "window_start_ms": window_start_ms,
                "window_end_ms": window_end_ms,
                METERING_WINDOW_STARTED_AT_METADATA_KEY: metering_window_started_at.isoformat(),
                METERING_WINDOW_ENDED_AT_METADATA_KEY: metering_window_ended_at.isoformat(),
            },
        )

    def _cost_per_ms(
        self,
        request: ContainerRequestContext,
        explicit_cost_per_ms: float | None,
    ) -> float | None:
        if explicit_cost_per_ms is not None:
            return explicit_cost_per_ms
        if self.cost_resolver is None:
            return None
        return self.cost_resolver.cost_per_ms(request)


def usage_record_kind(metric: WorkerUsageMetricName) -> tuple[UsageMetric, UsageUnit]:
    if metric is WorkerUsageMetricName.ContainerDuration:
        return (UsageMetric.ContainerDurationMilliseconds, UsageUnit.Milliseconds)
    if metric is WorkerUsageMetricName.ContainerCost:
        return (UsageMetric.ContainerCostCents, UsageUnit.Cents)
    if metric is WorkerUsageMetricName.Cpu:
        return (UsageMetric.CpuSeconds, UsageUnit.Seconds)
    if metric is WorkerUsageMetricName.Memory:
        return (UsageMetric.MemoryGibSeconds, UsageUnit.GibSeconds)
    if metric is WorkerUsageMetricName.Gpu:
        return (UsageMetric.GpuSeconds, UsageUnit.Seconds)
    if metric is WorkerUsageMetricName.ContainerDisk:
        return (UsageMetric.ContainerDiskByteSeconds, UsageUnit.ByteSeconds)
    if metric is WorkerUsageMetricName.CpuUsed:
        return (UsageMetric.CpuUsedCoreSeconds, UsageUnit.Seconds)
    if metric is WorkerUsageMetricName.MemoryRss:
        return (UsageMetric.MemoryRssByteSeconds, UsageUnit.ByteSeconds)
    if metric is WorkerUsageMetricName.MemorySwap:
        return (UsageMetric.MemorySwapByteSeconds, UsageUnit.ByteSeconds)
    if metric is WorkerUsageMetricName.GpuMemory:
        return (UsageMetric.GpuMemoryByteSeconds, UsageUnit.ByteSeconds)
    if metric is WorkerUsageMetricName.NetworkIngress:
        return (UsageMetric.NetworkIngressBytes, UsageUnit.Bytes)
    if metric is WorkerUsageMetricName.NetworkEgress:
        return (UsageMetric.NetworkEgressBytes, UsageUnit.Bytes)
    if metric is WorkerUsageMetricName.NetworkIngressPackets:
        return (UsageMetric.NetworkIngressPackets, UsageUnit.Count)
    if metric is WorkerUsageMetricName.NetworkEgressPackets:
        return (UsageMetric.NetworkEgressPackets, UsageUnit.Count)
    if metric is WorkerUsageMetricName.DiskRead:
        return (UsageMetric.DiskReadBytes, UsageUnit.Bytes)
    if metric is WorkerUsageMetricName.DiskWrite:
        return (UsageMetric.DiskWriteBytes, UsageUnit.Bytes)
    msg = f"unsupported worker usage metric: {metric}"
    raise ValueError(msg)


def _usage_window_bounds(
    *,
    duration_ms: int,
    window_start_ms: int,
    window_end_ms: int | None,
) -> tuple[int, int]:
    start = max(int(window_start_ms), 0)
    end = int(window_end_ms) if window_end_ms is not None else start + max(duration_ms, 0)
    if end <= start and duration_ms > 0:
        end = start + duration_ms
    return start, max(end, start)


def _utc_metering_window(
    *,
    started_at: datetime,
    ended_at: datetime,
) -> tuple[datetime, datetime]:
    if started_at.tzinfo is None or started_at.utcoffset() is None:
        raise ValueError("metering window start must include a timezone")
    if ended_at.tzinfo is None or ended_at.utcoffset() is None:
        raise ValueError("metering window end must include a timezone")
    utc_started_at = started_at.astimezone(UTC)
    utc_ended_at = ended_at.astimezone(UTC)
    if utc_ended_at <= utc_started_at:
        raise ValueError("metering window end must be after start")
    return utc_started_at, utc_ended_at
