from __future__ import annotations

from datetime import datetime

from pydantic import Field, JsonValue

from shared.autoscaler_state import AutoscalerTargetKind
from shared.compute_fleet import LeaseStatus, ResourceStatus
from shared.compute_policy import LAZYCLOUD_MACHINE_POOL, MachinePool
from shared.events import Event
from shared.http.base import HttpModel
from shared.http.stubs import StubResponse
from shared.image_building.authoring import ImageSpec
from shared.image_building.records import BuildStatus, ImageBuildPhase


class CronJobResponse(HttpModel):
    name: str
    cron: str
    deployment_id: str
    queue: str = "tasks"
    payload: JsonValue = None
    enabled: bool = True
    last_run_at: datetime | None = None
    next_run_at: datetime | None = None
    created_at: datetime
    updated_at: datetime


class CronJobListResponse(HttpModel):
    cron_jobs: list[CronJobResponse] = Field(default_factory=list)


class CronJobRunResponse(HttpModel):
    id: str
    workspace_id: str
    cron_job: str
    enqueued: bool
    message_id: str | None = None
    task_id: str | None = None
    reason: str | None = None
    created_at: datetime


class CronJobRunListResponse(HttpModel):
    data: list[CronJobRunResponse] = Field(default_factory=list)
    next: str = ""


class SchedulerContainerDispatchResponse(HttpModel):
    status: str
    container_id: str
    worker_id: str = ""
    reason: str = ""


class SchedulerContainerDispatchListResponse(HttpModel):
    dispatches: list[SchedulerContainerDispatchResponse] = Field(default_factory=list)


class AutoscalerStateResponse(HttpModel):
    name: str
    workspace_id: str
    source: str
    target_kind: AutoscalerTargetKind
    target_id: str
    deployment_id: str = ""
    app_id: str = ""
    current_count: int = 0
    desired_count: int = 0
    signal_name: str = ""
    signal_value: int = 0
    decision: str = ""
    reason: str = ""
    active: bool = True
    valid: bool = True
    lock_acquired: bool = True
    owner_lock_key: str = ""
    cooldown_until: datetime | None = None
    failed_container_count: int = 0
    error: str = ""
    updated_at: datetime


class AutoscalerStatusItemResponse(HttpModel):
    state: AutoscalerStateResponse
    stub_name: str = ""
    stub_kind: str = ""
    autoscaling_enabled: bool = True


class AutoscalerStatusListResponse(HttpModel):
    items: list[AutoscalerStatusItemResponse] = Field(default_factory=list)


class AutoscalerHistoryResponse(HttpModel):
    events: list[Event] = Field(default_factory=list)


class AutoscalerControlResponse(HttpModel):
    stub: StubResponse
    target_kind: AutoscalerTargetKind
    autoscaling_enabled: bool


class AutoscalerReconcileResponse(HttpModel):
    results: list[dict[str, JsonValue]] = Field(default_factory=list)


class ImageBuildRequest(HttpModel):
    image: ImageSpec = Field(default_factory=ImageSpec)
    tag: str | None = None


class ImageBuildResponse(HttpModel):
    id: str
    image: ImageSpec
    fingerprint: str
    image_id: str | None = None
    cache_key: str | None = None
    dockerfile: str | None = None
    context_digest: str | None = None
    status: BuildStatus = BuildStatus.Pending
    phase: ImageBuildPhase = ImageBuildPhase.Planning
    tag: str | None = None
    manifest_path: str | None = None
    published_ref: str | None = None
    artifact_path: str | None = None
    cache_metadata: dict[str, str] = Field(default_factory=dict)
    logs: list[str] = Field(default_factory=list)
    error: str | None = None
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None


class ImageBuildListResponse(HttpModel):
    builds: list[ImageBuildResponse] = Field(default_factory=list)


class AgentRegisterRequest(HttpModel):
    name: str = "agent"
    pool: MachinePool = MachinePool(LAZYCLOUD_MACHINE_POOL)
    version: str = "local"
    capacity: dict[str, int | float | str] = Field(default_factory=dict)
    labels: dict[str, str] = Field(default_factory=dict)


class AgentResponse(HttpModel):
    id: str
    name: str
    pool: MachinePool = MachinePool(LAZYCLOUD_MACHINE_POOL)
    status: ResourceStatus = ResourceStatus.Created
    version: str = "local"
    capacity: dict[str, int | float | str] = Field(default_factory=dict)
    labels: dict[str, str] = Field(default_factory=dict)
    install_command: str | None = None
    last_seen_at: datetime | None = None
    created_at: datetime
    updated_at: datetime


class AgentListResponse(HttpModel):
    agents: list[AgentResponse] = Field(default_factory=list)


class AgentLeaseRequest(HttpModel):
    resource_type: str
    resource_id: str
    ttl_seconds: int = 300


class AgentLeaseResponse(HttpModel):
    id: str
    agent_id: str
    resource_type: str
    resource_id: str
    status: LeaseStatus = LeaseStatus.Active
    expires_at: datetime
    created_at: datetime
    released_at: datetime | None = None


class AgentLeaseListResponse(HttpModel):
    leases: list[AgentLeaseResponse] = Field(default_factory=list)


__all__ = [
    "AgentLeaseListResponse",
    "AgentLeaseRequest",
    "AgentLeaseResponse",
    "AgentListResponse",
    "AgentRegisterRequest",
    "AgentResponse",
    "AutoscalerControlResponse",
    "AutoscalerHistoryResponse",
    "AutoscalerReconcileResponse",
    "AutoscalerStateResponse",
    "AutoscalerStatusItemResponse",
    "AutoscalerStatusListResponse",
    "CronJobListResponse",
    "CronJobResponse",
    "CronJobRunListResponse",
    "CronJobRunResponse",
    "ImageBuildListResponse",
    "ImageBuildRequest",
    "ImageBuildResponse",
    "SchedulerContainerDispatchListResponse",
    "SchedulerContainerDispatchResponse",
]
