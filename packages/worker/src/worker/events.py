from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import IntEnum, StrEnum
from math import isfinite

from pydantic import Field, JsonValue, field_validator, model_validator
from shared.container_requests import (
    DEFAULT_WORKSPACE_STORAGE_BASE_MOUNT_PATH,
    RequestMount,
    StopContainerReason,
)
from shared.contracts import ContractModel
from shared.realtime.contracts import ContainerMetricsData, ContainerMetricsPayload
from shared.usage import UsageBillingOwner

from worker.tools import NetworkIoCounters, ProcessIoCounters, WorkspaceStorageCredentials

WORKER_EVENT_HEARTBEAT_ID = "__heartbeat__"


class WorkerStreamEventKind(StrEnum):
    Heartbeat = "heartbeat"
    StopContainer = "stop-container"
    StopBuild = "stop-build"
    PurgeSourceCache = "purge-source-cache"
    Unknown = "unknown"


class WorkerStreamDecisionAction(StrEnum):
    Ignore = "ignore"
    StopContainer = "stop-container"
    CancelBuild = "cancel-build"
    PurgeSourceCache = "purge-source-cache"
    Warn = "warn"


class WorkerBuildCancelAction(StrEnum):
    Register = "register"
    Unregister = "unregister"
    Cancel = "cancel"


class ContainerExecutionPhase(StrEnum):
    PublishWorkerAddress = "publish-worker-address"
    HydrateCredentials = "hydrate-credentials"
    LoadImage = "load-image"
    AllocatePorts = "allocate-ports"
    SetupNetwork = "setup-network"
    PublishContainerRoutes = "publish-container-routes"
    SetupWorkspaceStorage = "setup-workspace-storage"
    SetupMounts = "setup-mounts"
    PrepareRootfs = "prepare-rootfs"
    AssignGpu = "assign-gpu"
    BuildSpec = "build-spec"
    PrepareRuntime = "prepare-runtime"
    PrepareSandboxDocker = "prepare-sandbox-docker"
    CompleteCheckpointStartup = "complete-checkpoint-startup"
    MarkRunning = "mark-running"
    RunRuntime = "run-runtime"
    HandleOom = "handle-oom"
    PublishExitEvent = "publish-exit-event"
    Finalize = "finalize"
    DelayedCleanup = "delayed-cleanup"


class ContainerExitCode(IntEnum):
    UnknownError = 1
    Success = 0
    OomKill = 137
    Sigterm = 143
    InvalidCustomImage = 555
    IncorrectImageArch = 556
    IncorrectImageOs = 557
    Scheduler = 558
    Ttl = 559
    User = 560
    Admin = 561
    Preempted = 562
    MemoryEvicted = 563
    """Its own code, and the reason this feature has one.

    A force-killed container exits 137, which is also `OomKill` -- the code
    that tells a customer their container exceeded its own memory limit. An
    evicted container did not: it was inside its ceiling and the platform
    stopped it because the machine ran short. Reporting both the same way is
    exactly the misattribution the stop reason exists to prevent.
    """


class WorkerPoolMode(StrEnum):
    Public = "public"
    Private = "private"


class WorkerUsageMetricName(StrEnum):
    ContainerDuration = "container_duration_milliseconds"
    CpuUsed = "cpu_used_core_seconds"
    MemoryRss = "memory_rss_byte_seconds"
    MemorySwap = "memory_swap_byte_seconds"
    GpuMemory = "gpu_memory_byte_seconds"
    NetworkIngress = "network_ingress_bytes"
    NetworkSent = "network_sent_bytes"
    NetworkEgress = "network_egress_bytes"
    NetworkIngressPackets = "network_ingress_packets"
    NetworkEgressPackets = "network_egress_packets"
    ContainerDisk = "container_disk_byte_seconds"
    DiskRead = "disk_read_bytes"
    DiskWrite = "disk_write_bytes"


class WorkerStreamEvent(ContractModel):
    event_id: str = ""
    kind: WorkerStreamEventKind = WorkerStreamEventKind.Unknown
    container_id: str = ""
    force: bool = False
    reason: StopContainerReason = StopContainerReason.Unknown


class StopContainerPlan(ContractModel):
    container_id: str
    force: bool = False
    reason: StopContainerReason = StopContainerReason.Unknown
    source: str = "worker-event-stream"


class WorkerStreamEventDecision(ContractModel):
    action: WorkerStreamDecisionAction
    event_id: str = ""
    stop_container: StopContainerPlan | None = None
    cancel_build_container_id: str = ""
    reason: str = ""
    warn: bool = False


class WorkerBuildCancelResult(ContractModel):
    action: WorkerBuildCancelAction
    container_id: str
    found: bool = False
    replaced: bool = False
    invoked: bool = False
    registered_count: int = 0
    reason: str = ""


class ContainerRequestContext(ContractModel):
    container_id: str
    image_id: str = ""
    # Digest the control plane resolved against this request's own workspace
    # authorization. Empty means no archive is authorized for the image.
    archive_sha256: str = Field(default="", pattern=r"^(?:[0-9a-f]{64})?$")
    preload_image: bool = False
    stub_id: str = ""
    stub_type: str = ""
    workspace_id: str = ""
    workspace_name: str = ""
    app_id: str = ""
    deployment_id: str = ""
    env: list[str] = Field(default_factory=list)
    mounts: list[RequestMount] = Field(default_factory=list)
    secret_names: list[str] = Field(default_factory=list)
    gateway_token_required: bool = False
    workspace_storage_required: bool = False
    workspace_storage_available: bool = False
    workspace_storage_credentials: WorkspaceStorageCredentials | None = None
    workspace_storage_base_mount_path: str = DEFAULT_WORKSPACE_STORAGE_BASE_MOUNT_PATH
    cpu_millicores: int = 0
    memory_mib: int = 0
    disk_limit_bytes: int = 0
    gpu: str = ""
    gpu_count: int = 0

    @field_validator("cpu_millicores", "memory_mib", "disk_limit_bytes", "gpu_count")
    @classmethod
    def non_negative_ints(cls, value: int) -> int:
        if value < 0:
            msg = "container request numeric values cannot be negative"
            raise ValueError(msg)
        return value


class ContainerEventPayload(ContractModel):
    id: str = ""
    container_id: str = ""
    stub_id: str = ""
    stub_type: str = ""
    workspace_id: str = ""
    app_id: str = ""
    task_id: str = ""
    cpu: int = 0
    gpu_count: int = 0
    worker_id: str = ""
    reason: str = ""
    source: str = ""
    message: str = ""
    attrs: dict[str, str] = Field(default_factory=dict)


class ContainerLifecyclePayload(ContractModel):
    id: str
    container_id: str = ""
    stub_id: str = ""
    stub_type: str = ""
    workspace_id: str = ""
    app_id: str = ""
    task_id: str = ""
    worker_id: str = ""
    start_time: datetime
    end_time: datetime
    duration_ms: int
    success: bool | None = None
    attrs: dict[str, str] = Field(default_factory=dict)


class GpuMemoryCounters(ContractModel):
    used_bytes: int = 0
    total_bytes: int = 0


class WorkerUsageMetricPlan(ContractModel):
    name: WorkerUsageMetricName
    labels: dict[str, JsonValue]
    value: float


class WorkerUsageEvidence(ContractModel):
    cpu_used_core_seconds: float = 0
    memory_rss_byte_seconds: float = 0
    memory_swap_byte_seconds: float = 0
    # Ephemeral container disk actually occupied, for the window.
    disk_used_byte_seconds: float = 0
    gpu_memory_byte_seconds: float = 0
    network_ingress_bytes: int = 0
    network_sent_bytes: int = 0
    network_egress_bytes: int = 0
    network_ingress_packets: int = 0
    network_egress_packets: int = 0
    disk_read_bytes: int = 0
    disk_write_bytes: int = 0

    @model_validator(mode="after")
    def non_negative_counters(self) -> WorkerUsageEvidence:
        values = (
            self.cpu_used_core_seconds,
            self.memory_rss_byte_seconds,
            self.memory_swap_byte_seconds,
            self.gpu_memory_byte_seconds,
            self.network_ingress_bytes,
            self.network_sent_bytes,
            self.network_egress_bytes,
            self.network_ingress_packets,
            self.network_egress_packets,
            self.disk_read_bytes,
            self.disk_write_bytes,
        )
        if any(not isfinite(value) or value < 0 for value in values):
            raise ValueError("worker usage evidence must be finite and nonnegative")
        return self

    def plus(self, other: WorkerUsageEvidence) -> WorkerUsageEvidence:
        """Accumulate one sample into a metering window.

        Summed field-wise off the model itself: enumerating fields by hand meant a
        newly added counter was silently dropped from every window it appeared in.
        """
        totals = {
            name: getattr(self, name) + getattr(other, name) for name in type(self).model_fields
        }
        return WorkerUsageEvidence(**totals)


@dataclass(slots=True)
class WorkerBuildCancelRegistry:
    _callbacks: dict[str, Callable[[], None]] = field(default_factory=dict)

    def register(self, container_id: str, cancel: Callable[[], None]) -> WorkerBuildCancelResult:
        if not container_id:
            return WorkerBuildCancelResult(
                action=WorkerBuildCancelAction.Register,
                container_id=container_id,
                registered_count=len(self._callbacks),
                reason="container id is required",
            )
        replaced = container_id in self._callbacks
        self._callbacks[container_id] = cancel
        return WorkerBuildCancelResult(
            action=WorkerBuildCancelAction.Register,
            container_id=container_id,
            found=True,
            replaced=replaced,
            registered_count=len(self._callbacks),
            reason="build cancel registered",
        )

    def unregister(self, container_id: str) -> WorkerBuildCancelResult:
        found = self._callbacks.pop(container_id, None) is not None
        return WorkerBuildCancelResult(
            action=WorkerBuildCancelAction.Unregister,
            container_id=container_id,
            found=found,
            registered_count=len(self._callbacks),
            reason="build cancel unregistered" if found else "build cancel not registered",
        )

    def cancel(self, container_id: str) -> WorkerBuildCancelResult:
        callback = self._callbacks.get(container_id)
        if callback is None:
            return WorkerBuildCancelResult(
                action=WorkerBuildCancelAction.Cancel,
                container_id=container_id,
                registered_count=len(self._callbacks),
                reason="build cancel not registered",
            )
        callback()
        return WorkerBuildCancelResult(
            action=WorkerBuildCancelAction.Cancel,
            container_id=container_id,
            found=True,
            invoked=True,
            registered_count=len(self._callbacks),
            reason="build cancel invoked",
        )


def decide_worker_stream_event(event: WorkerStreamEvent | None) -> WorkerStreamEventDecision:
    if event is None:
        return WorkerStreamEventDecision(
            action=WorkerStreamDecisionAction.Ignore,
            reason="nil worker event",
        )
    if event.kind is WorkerStreamEventKind.Heartbeat or event.event_id == WORKER_EVENT_HEARTBEAT_ID:
        return WorkerStreamEventDecision(
            action=WorkerStreamDecisionAction.Ignore,
            event_id=event.event_id,
            reason="heartbeat",
        )
    if event.kind is WorkerStreamEventKind.StopContainer:
        return WorkerStreamEventDecision(
            action=WorkerStreamDecisionAction.StopContainer,
            event_id=event.event_id,
            stop_container=StopContainerPlan(
                container_id=event.container_id,
                force=event.force,
                reason=event.reason,
            ),
            reason="stop container event",
        )
    if event.kind is WorkerStreamEventKind.StopBuild:
        return WorkerStreamEventDecision(
            action=WorkerStreamDecisionAction.CancelBuild,
            event_id=event.event_id,
            cancel_build_container_id=event.container_id,
            reason="stop build event",
        )
    if event.kind is WorkerStreamEventKind.PurgeSourceCache:
        return WorkerStreamEventDecision(
            action=WorkerStreamDecisionAction.PurgeSourceCache,
            event_id=event.event_id,
            reason="source cache reconciliation wake",
        )
    return WorkerStreamEventDecision(
        action=WorkerStreamDecisionAction.Warn,
        event_id=event.event_id,
        reason="unknown worker event",
        warn=True,
    )


def normalize_stop_reason(reason: str | StopContainerReason | None) -> StopContainerReason:
    if isinstance(reason, StopContainerReason):
        return reason
    normalized = (reason or "").strip()
    if not normalized:
        return StopContainerReason.Unknown
    try:
        return StopContainerReason(normalized.upper())
    except ValueError:
        return StopContainerReason.Unknown


def normalize_container_exit_code(
    exit_code: int,
    *,
    stop_reason: StopContainerReason = StopContainerReason.Unknown,
    oom_killed: bool = False,
) -> int:
    reason = normalize_stop_reason(stop_reason)
    if reason is StopContainerReason.Scheduler:
        return int(ContainerExitCode.Scheduler)
    if reason is StopContainerReason.Ttl:
        return int(ContainerExitCode.Ttl)
    if reason is StopContainerReason.User:
        return int(ContainerExitCode.User)
    if reason is StopContainerReason.Preempted:
        return int(ContainerExitCode.Preempted)
    if reason is StopContainerReason.Admin:
        return int(ContainerExitCode.Admin)
    if reason is StopContainerReason.MemoryEvicted:
        # Before the `oom_killed` branch: an eviction is a SIGKILL and the
        # runtime reports it as an OOM, which is the confusion being avoided.
        return int(ContainerExitCode.MemoryEvicted)
    if oom_killed:
        return int(ContainerExitCode.OomKill)
    if exit_code < 0:
        return int(ContainerExitCode.UnknownError)
    return exit_code


def container_exit_reason(
    exit_code: int,
    *,
    stop_reason: StopContainerReason = StopContainerReason.Unknown,
    oom_killed: bool = False,
) -> str:
    reason = normalize_stop_reason(stop_reason)
    if reason is not StopContainerReason.Unknown:
        return reason.value
    if oom_killed:
        return "OOM"
    if exit_code == int(ContainerExitCode.Success):
        return "COMPLETED"
    if exit_code == int(ContainerExitCode.OomKill):
        return "SIGKILL"
    return "ERROR"


def event_stop_reason(stop_reason: StopContainerReason) -> str:
    reason = normalize_stop_reason(stop_reason)
    return "" if reason is StopContainerReason.Unknown else reason.value


def task_id_from_env(env: list[str]) -> str:
    for entry in env:
        key, separator, value = entry.partition("=")
        if separator and key == "TASK_ID":
            return value
    return ""


def populate_container_event(
    event: ContainerEventPayload,
    request: ContainerRequestContext | None,
    *,
    worker_id: str = "",
) -> ContainerEventPayload:
    if request is None:
        return event.model_copy(update={"worker_id": event.worker_id or worker_id})
    return event.model_copy(
        update={
            "container_id": event.container_id or request.container_id,
            "stub_id": event.stub_id or request.stub_id,
            "stub_type": event.stub_type or request.stub_type,
            "workspace_id": event.workspace_id or request.workspace_id,
            "app_id": event.app_id or request.app_id,
            "task_id": event.task_id or task_id_from_env(request.env),
            "cpu": event.cpu or request.cpu_millicores,
            "gpu_count": event.gpu_count or request.gpu_count,
            "worker_id": event.worker_id or worker_id,
        }
    )


def populate_container_lifecycle(
    lifecycle: ContainerLifecyclePayload,
    request: ContainerRequestContext | None,
    *,
    worker_id: str = "",
) -> ContainerLifecyclePayload:
    if request is None:
        return lifecycle.model_copy(update={"worker_id": lifecycle.worker_id or worker_id})
    return lifecycle.model_copy(
        update={
            "container_id": lifecycle.container_id or request.container_id,
            "stub_id": lifecycle.stub_id or request.stub_id,
            "stub_type": lifecycle.stub_type or request.stub_type,
            "workspace_id": lifecycle.workspace_id or request.workspace_id,
            "app_id": lifecycle.app_id or request.app_id,
            "task_id": lifecycle.task_id or task_id_from_env(request.env),
            "worker_id": lifecycle.worker_id or worker_id,
        }
    )


def container_lifecycle_from_duration(
    lifecycle_id: str,
    request: ContainerRequestContext,
    *,
    started_at: datetime,
    duration: timedelta,
    success: bool,
    attrs: dict[str, str] | None = None,
    worker_id: str = "",
) -> ContainerLifecyclePayload:
    end_time = started_at + duration
    lifecycle = ContainerLifecyclePayload(
        id=lifecycle_id,
        start_time=started_at,
        end_time=end_time,
        duration_ms=int(duration.total_seconds() * 1000),
        success=success,
        attrs=attrs or {},
    )
    return populate_container_lifecycle(lifecycle, request, worker_id=worker_id)


def cpu_percent(cpu_used_millicores: int, cpu_total_millicores: int) -> float:
    if cpu_total_millicores <= 0:
        return 0.0
    return cpu_used_millicores * 100 / cpu_total_millicores


def build_container_metrics_payload(
    *,
    worker_id: str,
    request: ContainerRequestContext,
    sample_interval_ms: int,
    cpu_used_millicores: int,
    memory_rss_bytes: int,
    memory_vms_bytes: int = 0,
    memory_swap_bytes: int = 0,
    process_io: ProcessIoCounters | None = None,
    network_io: NetworkIoCounters | None = None,
    gpu_memory: GpuMemoryCounters | None = None,
    disk_used_bytes: int = 0,
) -> ContainerMetricsPayload:
    process = process_io or ProcessIoCounters()
    network = network_io or NetworkIoCounters()
    gpu = gpu_memory or GpuMemoryCounters()
    return ContainerMetricsPayload(
        worker_id=worker_id,
        container_id=request.container_id,
        workspace_id=request.workspace_id,
        stub_id=request.stub_id,
        stub_type=request.stub_type,
        app_id=request.app_id,
        cpu=request.cpu_millicores,
        gpu_count=request.gpu_count,
        metrics=ContainerMetricsData(
            sample_interval_ms=sample_interval_ms,
            cpu_used=cpu_used_millicores,
            cpu_total=request.cpu_millicores,
            cpu_pct=cpu_percent(cpu_used_millicores, request.cpu_millicores),
            memory_rss_bytes=memory_rss_bytes,
            memory_vms_bytes=memory_vms_bytes,
            memory_swap_bytes=memory_swap_bytes,
            memory_total_bytes=request.memory_mib * 1024 * 1024,
            disk_read_bytes=process.disk_read_bytes,
            disk_write_bytes=process.disk_write_bytes,
            disk_used_bytes=disk_used_bytes,
            disk_total_bytes=request.disk_limit_bytes,
            network_recv_bytes=network.bytes_recv,
            network_sent_bytes=network.bytes_sent,
            network_recv_packets=network.packets_recv,
            network_sent_packets=network.packets_sent,
            gpu_memory_used_bytes=gpu.used_bytes,
            gpu_memory_total_bytes=gpu.total_bytes,
            gpu_type=request.gpu,
        ),
    )


def plan_worker_usage_metrics(
    *,
    worker_id: str,
    request: ContainerRequestContext,
    duration_ms: int,
    billing_owner: UsageBillingOwner,
    pool_mode: WorkerPoolMode = WorkerPoolMode.Public,
    evidence: WorkerUsageEvidence | None = None,
    measurement_complete: bool = False,
) -> tuple[WorkerUsageMetricPlan, ...]:
    labels: dict[str, JsonValue] = {
        "container_id": request.container_id,
        "worker_id": worker_id,
        "stub_id": request.stub_id,
        "app_id": request.app_id,
        "deployment_id": request.deployment_id,
        "workspace_id": request.workspace_id,
        "cpu_millicores": request.cpu_millicores,
        "mem_mb": request.memory_mib,
        "gpu": request.gpu,
        "gpu_count": request.gpu_count,
        "duration_ms": duration_ms,
        "pool_mode": pool_mode.value,
        # Telemetry, deciding nothing. What this worker believes it is, beside the
        # rest of what it believes about itself; cost comes from the placement the
        # control plane recorded in `container_billing_shapes`, which a worker
        # cannot write.
        "billing_owner": billing_owner.value,
    }
    measured = evidence or WorkerUsageEvidence()
    # The window and what was measured over it. What the container reserved is
    # not restated as its own metric: the ledger prices the reservation from the
    # placement the control plane recorded, and this worker's view of it is a
    # label on a machine a customer has root on.
    plans = [
        WorkerUsageMetricPlan(
            name=WorkerUsageMetricName.ContainerDuration,
            labels=labels,
            value=float(duration_ms),
        ),
    ]
    if measured.disk_used_byte_seconds > 0:
        plans.append(
            WorkerUsageMetricPlan(
                name=WorkerUsageMetricName.ContainerDisk,
                labels=labels,
                value=measured.disk_used_byte_seconds,
            )
        )
    evidence_values = (
        (WorkerUsageMetricName.CpuUsed, measured.cpu_used_core_seconds),
        (WorkerUsageMetricName.MemoryRss, measured.memory_rss_byte_seconds),
        (WorkerUsageMetricName.MemorySwap, measured.memory_swap_byte_seconds),
        (WorkerUsageMetricName.GpuMemory, measured.gpu_memory_byte_seconds),
        (WorkerUsageMetricName.NetworkIngress, measured.network_ingress_bytes),
        (WorkerUsageMetricName.NetworkSent, measured.network_sent_bytes),
        (WorkerUsageMetricName.NetworkEgress, measured.network_egress_bytes),
        (WorkerUsageMetricName.NetworkIngressPackets, measured.network_ingress_packets),
        (WorkerUsageMetricName.NetworkEgressPackets, measured.network_egress_packets),
        (WorkerUsageMetricName.DiskRead, measured.disk_read_bytes),
        (WorkerUsageMetricName.DiskWrite, measured.disk_write_bytes),
    )
    plans.extend(
        WorkerUsageMetricPlan(name=name, labels=labels, value=float(value))
        for name, value in evidence_values
        if value > 0
        or (
            measurement_complete
            and name in {WorkerUsageMetricName.CpuUsed, WorkerUsageMetricName.MemoryRss}
        )
    )
    return tuple(plans)
