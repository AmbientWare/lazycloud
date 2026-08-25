from __future__ import annotations

from datetime import datetime

from pydantic import Field, model_validator

from shared.capacity import (
    CAPACITY_OWNER_ID_PATTERN,
    CapacityOwnerIdentity,
    CapacityOwnerKind,
    CapacityOwnerSource,
)
from shared.compute_enrollment import (
    AgentCapacityState,
    ComputePreflightCheck,
    MachineReadinessPhase,
)
from shared.compute_fleet import ResourceStatus
from shared.compute_policy import LAZYCLOUD_MACHINE_POOL, ComputeUnitPhase, MachinePool
from shared.container_requests import OciRuntimeName, StopContainerReason
from shared.containers import ContainerStatus
from shared.http.apps import AppResponse
from shared.http.base import HttpModel
from shared.http.deployments import DeploymentResponse
from shared.http.stubs import StubResponse
from shared.tasks import TaskStatus


class UnitPolicy(HttpModel):
    """The worker shape and scaling policy a provisioning unit publishes.

    Stated here rather than inherited from the durable record: the wire
    vocabulary is the contract, and a unit column that stops being published
    must not silently disappear from the payload with it.
    """

    initial_machines: int = Field(default=0, ge=0)
    min_machines: int = Field(default=0, ge=0)
    max_machines: int = Field(default=1, ge=0)
    scaling_enabled: bool = False
    default_eligible: bool = False
    priority: int = Field(default=0, ge=-(2**31), le=2**31 - 1)
    """Preference for this unit over another that could serve the same work.

    Higher is preferred. Work fills the highest tier that fits before any of the
    next, and inside a tier placement is unchanged.

    A provider unit's machines read it when they launch, so retuning it reaches
    the fleet as machines turn over rather than at once. A joined machine is
    never replaced, so its workers converge on the next reconcile instead.
    """
    min_free_cpu_millicores: int = Field(default=0, ge=0)
    min_free_memory_mib: int = Field(default=0, ge=0)
    min_free_gpu_count: int = Field(default=0, ge=0)
    worker_cpu_millicores: int = Field(default=0, ge=0)
    worker_memory_mib: int = Field(default=0, ge=0)
    worker_gpu_type: str = Field(default="", max_length=160)
    worker_gpu_count: int = Field(default=0, ge=0)
    worker_runtimes: tuple[str, ...] = (OciRuntimeName.Runsc.value,)
    worker_preemptible: bool = False
    idle_drain_timeout_seconds: int = Field(default=300, ge=60, le=86_400)
    scale_up_cooldown_seconds: int = Field(default=5, ge=0, le=86_400)
    scale_down_cooldown_seconds: int = Field(default=60, ge=0, le=86_400)
    registration_timeout_seconds: int = Field(default=600, ge=30, le=3_600)


class UnitCreateRequest(UnitPolicy):
    name: str
    pool: MachinePool = MachinePool("")
    provider: str = "local"
    labels: dict[str, str] = Field(default_factory=dict)


class UnitResponse(UnitPolicy):
    # The value every other unit route is keyed by. A name is deliberately not
    # usable in its place, so without this an operator cannot address a unit at
    # all.
    id: str
    capacity_owner_id: str = Field(pattern=CAPACITY_OWNER_ID_PATTERN)
    capacity_owner_kind: CapacityOwnerKind
    capacity_owner_source: CapacityOwnerSource
    name: str
    pool: MachinePool
    provider: str = "local"
    labels: dict[str, str] = Field(default_factory=dict)
    created_at: datetime

    @model_validator(mode="after")
    def validate_capacity_owner(self) -> UnitResponse:
        CapacityOwnerIdentity.model_validate(
            {
                "capacity_owner_id": self.capacity_owner_id,
                "capacity_owner_kind": self.capacity_owner_kind,
                "capacity_owner_source": self.capacity_owner_source,
            }
        )
        return self


class UnitListResponse(HttpModel):
    pools: list[UnitResponse] = Field(default_factory=list)


class UnitScaleRequest(HttpModel):
    desired_machines: int = Field(ge=0)


class UnitScaleResponse(HttpModel):
    id: str
    name: str
    desired_machines: int = Field(ge=0)
    max_machines: int = Field(ge=0)
    observed_machines: int = Field(ge=0)
    phase: ComputeUnitPhase
    status: str
    degraded_reason: str | None = None


class MachineCreateRequest(HttpModel):
    provider: str = "local"
    cpu: float | None = None
    memory: str | None = None
    gpu: str | None = None
    address: str | None = None
    labels: dict[str, str] = Field(default_factory=dict)


class MachineResponse(HttpModel):
    id: str
    pool: MachinePool = MachinePool(LAZYCLOUD_MACHINE_POOL)
    provider: str = "local"
    status: ResourceStatus = ResourceStatus.Created
    cpu: float | None = None
    memory: str | None = None
    gpu: str | None = None
    address: str | None = None
    labels: dict[str, str] = Field(default_factory=dict)
    created_at: datetime
    updated_at: datetime


class MachineListResponse(HttpModel):
    machines: list[MachineResponse] = Field(default_factory=list)


class MachineJoinCommandRequest(HttpModel):
    """Request the join command for a workspace's self-hosted fleet.

    Naming a pool creates it: a caller may join machines into any group they
    choose, including one an auto-scaling unit already feeds. Left empty, the
    workspace's implicit self-hosted fleet answers.
    """

    ttl: str = ""
    pool: MachinePool = MachinePool(Field(default="", max_length=240))
    gpu: list[str] = Field(default_factory=list)


class MachineJoinCommandResponse(HttpModel):
    command: str = ""
    expires_at: datetime


class MachineJoinTokenResponse(HttpModel):
    """The same credential the join command embeds, for a machine-readable caller.

    A process that has to write the token to a file should not have to parse it
    back out of a shell string.
    """

    token: str
    expires_at: datetime


class WorkerContainerResponse(HttpModel):
    container_id: str
    workspace_id: str = ""
    stub_id: str = ""
    status: str = ""
    scheduled_at: datetime
    started_at: datetime | None = None


class WorkerResponse(HttpModel):
    id: str
    status: str
    pool: MachinePool
    machine_id: str = ""
    gpu: str = ""
    runtime: str = ""
    total_cpu: int = 0
    total_memory: int = 0
    total_gpu_count: int = 0
    free_cpu: int = 0
    free_memory: int = 0
    free_gpu_count: int = 0
    resource_version: int = 0
    requires_pool_selector: bool = False
    preemptible: bool = False
    created_at: datetime
    updated_at: datetime
    active_containers: list[WorkerContainerResponse] = Field(default_factory=list)


class WorkerListResponse(HttpModel):
    workers: list[WorkerResponse] = Field(default_factory=list)


class UnitJoinTokenRequest(HttpModel):
    ttl: str = ""


class UnitJoinTokenResponse(HttpModel):
    token: str
    expires_at: datetime


class UnitJoinCommandRequest(HttpModel):
    ttl: str = ""


class UnitJoinCommandResponse(HttpModel):
    command: str
    expires_at: datetime


class UnitMachineMetricsResponse(HttpModel):
    total_cpu_available: int = 0
    total_memory_available: int = 0
    cpu_utilization_pct: float = 0.0
    memory_utilization_pct: float = 0.0
    worker_count: int = 0
    container_count: int = 0
    free_gpu_count: int = 0
    cache_usage_pct: float = 0.0
    cache_capacity: int = 0
    cache_memory_usage: int = 0
    cache_cpu_usage: float = 0.0
    memory_used_mb: int = 0
    memory_total_mb: int = 0
    disk_used_mb: int = 0
    disk_total_mb: int = 0
    disk_usage_pct: float = 0.0


class UnitMachineResponse(HttpModel):
    id: str
    cpu: int = 0
    memory: int = 0
    gpu: str = ""
    gpu_count: int = 0
    status: str = ""
    pool: MachinePool
    provider_name: str = "agent"
    readiness_phase: MachineReadinessPhase = MachineReadinessPhase.Joining
    readiness_message: str = "Waiting for the agent to connect"
    schedulable: bool = False
    capacity_state: AgentCapacityState = AgentCapacityState.Available
    capacity_reason: str = ""
    capacity_observed_at: datetime | None = None
    capacity_notice_at: datetime | None = None
    preflight_checks: list[ComputePreflightCheck] = Field(default_factory=list)
    remediation: list[str] = Field(default_factory=list)
    last_seen_at: datetime | None = None
    created_at: datetime | None = None
    agent_version: str = ""
    machine_metrics: UnitMachineMetricsResponse = Field(default_factory=UnitMachineMetricsResponse)


class UnitMachineListResponse(HttpModel):
    data: list[UnitMachineResponse] = Field(default_factory=list)
    next: str = ""


class WorkerDrainResponse(HttpModel):
    worker: WorkerResponse
    stopped_container_ids: list[str] = Field(default_factory=list)


class ContainerRunRequest(HttpModel):
    name: str = "container"
    image: str
    command: str | list[str]
    cwd: str | None = None
    env: dict[str, str] = Field(default_factory=dict)
    ports: dict[str, int] = Field(default_factory=dict)
    timeout_seconds: int | None = None


class ContainerResponse(HttpModel):
    id: str
    name: str
    image: str
    command: list[str]
    workspace_id: str
    stub_id: str | None = None
    app_id: str | None = None
    machine_id: str | None = None
    worker_id: str | None = None
    runtime_machine_id: str = ""
    runtime_worker_id: str = ""
    task_id: str | None = None
    status: ContainerStatus = ContainerStatus.Pending
    pid: int | None = None
    exit_code: int | None = None
    termination_reason: StopContainerReason = StopContainerReason.Unknown
    cwd: str | None = None
    ports: dict[str, int] = Field(default_factory=dict)
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None


class ContainerActionCapabilitiesResponse(HttpModel):
    can_stop: bool = False
    can_shell: bool = False
    can_create_image: bool = False
    can_snapshot_memory: bool = False


class ContainerDetailResponse(ContainerResponse):
    app: AppResponse | None = None
    workload: StubResponse | None = None
    deployment: DeploymentResponse | None = None
    run_name: str | None = None
    run_status: TaskStatus | None = None
    expires_at: datetime | None = None
    actions: ContainerActionCapabilitiesResponse = Field(
        default_factory=ContainerActionCapabilitiesResponse
    )


class ContainerWithAppResponse(HttpModel):
    container: ContainerResponse
    app_id: str = ""


class ContainerWithAppPageResponse(HttpModel):
    data: list[ContainerWithAppResponse] = Field(default_factory=list)
    next: str = ""


class ContainerStopAllResponse(HttpModel):
    message: str
    containers: list[ContainerResponse] = Field(default_factory=list)


__all__ = [
    "ContainerActionCapabilitiesResponse",
    "ContainerDetailResponse",
    "ContainerResponse",
    "ContainerRunRequest",
    "ContainerStopAllResponse",
    "ContainerWithAppPageResponse",
    "ContainerWithAppResponse",
    "MachineCreateRequest",
    "MachineJoinCommandRequest",
    "MachineJoinCommandResponse",
    "MachineListResponse",
    "MachineResponse",
    "UnitCreateRequest",
    "UnitJoinCommandRequest",
    "UnitJoinCommandResponse",
    "UnitJoinTokenRequest",
    "UnitJoinTokenResponse",
    "UnitListResponse",
    "UnitMachineListResponse",
    "UnitMachineMetricsResponse",
    "UnitMachineResponse",
    "UnitPolicy",
    "UnitResponse",
    "UnitScaleRequest",
    "UnitScaleResponse",
    "WorkerContainerResponse",
    "WorkerDrainResponse",
    "WorkerListResponse",
    "WorkerResponse",
]
