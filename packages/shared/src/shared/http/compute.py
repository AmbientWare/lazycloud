from __future__ import annotations

from datetime import datetime

from pydantic import Field, model_validator

from shared.capacity import (
    CAPACITY_OWNER_ID_PATTERN,
    CapacityOwnerIdentity,
    CapacityOwnerKind,
    CapacityOwnerSource,
    CapacityPoolPolicy,
)
from shared.compute_enrollment import (
    AgentCapacityState,
    ComputePreflightCheck,
    MachineReadinessPhase,
)
from shared.compute_fleet import ResourceStatus
from shared.compute_policy import ComputePoolPhase
from shared.container_requests import StopContainerReason
from shared.containers import ContainerStatus
from shared.http.apps import AppResponse
from shared.http.base import HttpModel
from shared.http.deployments import DeploymentResponse
from shared.http.stubs import StubResponse
from shared.tasks import TaskStatus


class PoolCreateRequest(HttpModel, CapacityPoolPolicy):
    name: str
    provider: str = "local"
    labels: dict[str, str] = Field(default_factory=dict)


class PoolResponse(HttpModel, CapacityPoolPolicy):
    capacity_owner_id: str = Field(pattern=CAPACITY_OWNER_ID_PATTERN)
    capacity_owner_kind: CapacityOwnerKind
    capacity_owner_source: CapacityOwnerSource
    name: str
    provider: str = "local"
    labels: dict[str, str] = Field(default_factory=dict)
    created_at: datetime

    @model_validator(mode="after")
    def validate_capacity_owner(self) -> PoolResponse:
        CapacityOwnerIdentity.model_validate(
            {
                "capacity_owner_id": self.capacity_owner_id,
                "capacity_owner_kind": self.capacity_owner_kind,
                "capacity_owner_source": self.capacity_owner_source,
            }
        )
        return self


class PoolListResponse(HttpModel):
    pools: list[PoolResponse] = Field(default_factory=list)


class PoolScaleRequest(HttpModel):
    desired_machines: int = Field(ge=0)


class PoolScaleResponse(HttpModel):
    name: str
    desired_machines: int = Field(ge=0)
    max_machines: int = Field(ge=0)
    observed_machines: int = Field(ge=0)
    phase: ComputePoolPhase
    status: str
    degraded_reason: str | None = None


class MachineCreateRequest(HttpModel):
    pool: str = "default"
    provider: str = "local"
    cpu: float | None = None
    memory: str | None = None
    gpu: str | None = None
    address: str | None = None
    labels: dict[str, str] = Field(default_factory=dict)


class MachineRegisterRequest(HttpModel):
    token: str = ""
    machine_id: str
    hostname: str = ""
    provider_name: str = "local"
    pool_name: str = "default"
    cpu: str = ""
    memory: str = ""
    gpu_count: str = "0"
    private_ip: str = ""


class MachineResponse(HttpModel):
    id: str
    pool: str = "default"
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


class MachineGpuCountsResponse(HttpModel):
    gpus: dict[str, int] = Field(default_factory=dict)


class MachineJoinCommandRequest(HttpModel):
    """Request the join command for the workspace's implicit self-hosted fleet.

    The server resolves or creates the fleet; callers never name a pool.
    """

    ttl: str = ""
    gpu: list[str] = Field(default_factory=list)


class MachineJoinCommandResponse(HttpModel):
    command: str = ""
    expires_at: datetime


class MachineRemoteConfigResponse(HttpModel):
    endpoint: str = "local"
    state_home: str
    pools: list[str] = Field(default_factory=list)
    providers: list[str] = Field(default_factory=list)


class MachineRegisterResponse(HttpModel):
    machine: MachineResponse
    config: MachineRemoteConfigResponse


class MachineConfigResponse(HttpModel):
    config: MachineRemoteConfigResponse


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
    pool_name: str
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


class PoolOfferQuery(HttpModel):
    provider: list[str] = Field(default_factory=list)
    region: list[str] = Field(default_factory=list)
    gpu: list[str] = Field(default_factory=list)
    node_count: int = Field(default=1, ge=1)
    ttl: str = ""
    max_spend: float = Field(default=0.0, ge=0.0)
    min_reliability: float = Field(default=0.0, ge=0.0, le=1.0)
    offer_id: str = ""


class PoolOfferResponse(HttpModel):
    id: str
    provider: str
    instance_type: str
    region: str
    gpu: str = ""
    gpu_count: int = 0
    cpu_millicores: int = 0
    memory_mb: int = 0
    hourly_cost_micros: int = 0
    reliability: float = 1.0
    available: int = 0
    storage_mb: int = 0
    cloud: str = ""
    node_count: int = 1
    display_name: str = ""
    category: str = ""
    region_display_name: str = ""
    latitude: float = 0.0
    longitude: float = 0.0


class PoolOfferListResponse(HttpModel):
    data: list[PoolOfferResponse] = Field(default_factory=list)
    next: str = ""


class PoolCapacityLaunchRequest(PoolOfferQuery):
    pass


class PoolCapacityExtendRequest(HttpModel):
    ttl: str
    max_spend: float = Field(gt=0.0)


class PoolProviderInstanceResponse(HttpModel):
    id: str
    provider: str = ""
    offer_id: str = ""
    status: str = ""
    gpu_count: int = 0
    hourly_cost_micros: int = 0
    created_at: datetime | None = None
    expires_at: datetime | None = None
    machine_id: str = ""
    region: str = ""
    node_count: int = 0
    instance_type: str = ""


class PoolCapacityResponse(HttpModel):
    name: str
    selector: str = ""
    reservations: list[PoolProviderInstanceResponse] = Field(default_factory=list)
    committed_spend_micros: int = 0
    max_spend_micros: int = 0
    status: str = "active"
    expires_at: datetime | None = None
    reserved_nodes: int = 0


class PoolJoinTokenRequest(HttpModel):
    ttl: str = ""


class PoolJoinTokenResponse(HttpModel):
    token: str
    expires_at: datetime


class PoolJoinCommandRequest(HttpModel):
    ttl: str = ""


class PoolJoinCommandResponse(HttpModel):
    command: str
    expires_at: datetime


class PoolMachineMetricsResponse(HttpModel):
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


class PoolMachineResponse(HttpModel):
    id: str
    cpu: int = 0
    memory: int = 0
    gpu: str = ""
    gpu_count: int = 0
    status: str = ""
    pool_name: str
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
    machine_metrics: PoolMachineMetricsResponse = Field(default_factory=PoolMachineMetricsResponse)


class PoolMachineListResponse(HttpModel):
    data: list[PoolMachineResponse] = Field(default_factory=list)
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
    "MachineConfigResponse",
    "MachineCreateRequest",
    "MachineGpuCountsResponse",
    "MachineJoinCommandRequest",
    "MachineJoinCommandResponse",
    "MachineListResponse",
    "MachineRegisterRequest",
    "MachineRegisterResponse",
    "MachineRemoteConfigResponse",
    "MachineResponse",
    "PoolCapacityExtendRequest",
    "PoolCapacityLaunchRequest",
    "PoolCapacityResponse",
    "PoolCreateRequest",
    "PoolJoinCommandRequest",
    "PoolJoinCommandResponse",
    "PoolJoinTokenRequest",
    "PoolJoinTokenResponse",
    "PoolListResponse",
    "PoolMachineListResponse",
    "PoolMachineMetricsResponse",
    "PoolMachineResponse",
    "PoolOfferListResponse",
    "PoolOfferQuery",
    "PoolOfferResponse",
    "PoolProviderInstanceResponse",
    "PoolResponse",
    "PoolScaleRequest",
    "PoolScaleResponse",
    "WorkerContainerResponse",
    "WorkerDrainResponse",
    "WorkerListResponse",
    "WorkerResponse",
]
