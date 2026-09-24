from __future__ import annotations

from datetime import datetime

from pydantic import Field

from shared.deployment_records import CpuRequest, MemoryRequest
from shared.deployments import DeploymentKind, DevboxPhase, DevboxState, PodRole
from shared.disks import DiskStatus
from shared.http.base import HttpModel
from shared.http.stubs import StubResponse
from shared.placement import AvailabilityZone, ProductRegion


class DeploymentResourcesResponse(HttpModel):
    region: ProductRegion | None = Field(default=None, exclude_if=lambda value: value is None)
    availability_zone: AvailabilityZone = ""
    preemptible: bool = False
    cpu: CpuRequest | None = None
    memory: MemoryRequest | None = None
    disk: str | None = None
    gpu: list[str] = Field(default_factory=list)
    gpu_count: int = 0
    timeout_seconds: int | None = None
    concurrency: int = Field(default=1, gt=0)
    keep_warm: int | None = Field(default=None, ge=-1)


class DeploymentSpecResponse(HttpModel):
    """Operator-safe deployment configuration for workload detail views."""

    resources: DeploymentResourcesResponse = Field(default_factory=DeploymentResourcesResponse)
    route: str | None = None
    methods: list[str] = Field(default_factory=list)
    cron: str | None = None
    command: list[str] = Field(default_factory=list)
    ports: dict[str, int] = Field(default_factory=dict)
    machine: str = ""


class DeploymentActionCapabilitiesResponse(HttpModel):
    can_start: bool = False
    can_stop: bool = False
    can_scale: bool = False
    can_delete: bool = False


class DeploymentScalingResponse(HttpModel):
    min_replicas: int = Field(ge=0)
    max_replicas: int = Field(ge=0)


class DeploymentScaleRequest(HttpModel):
    replicas: int = Field(ge=0)


class DeploymentResponse(HttpModel):
    id: str
    name: str
    kind: DeploymentKind
    role: PodRole | None = None
    """What a pod is for; unset for every other kind."""

    app_id: str | None = None
    stub_id: str | None = None
    version: int = 1
    spec: DeploymentSpecResponse = Field(default_factory=DeploymentSpecResponse)
    active: bool = True
    deleted_at: datetime | None = None
    created_at: datetime
    updated_at: datetime
    scaling: DeploymentScalingResponse | None = None
    actions: DeploymentActionCapabilitiesResponse = Field(
        default_factory=DeploymentActionCapabilitiesResponse
    )


class DevboxDiskResponse(HttpModel):
    name: str
    size_bytes: int = Field(gt=0)
    stored_bytes: int = Field(ge=0)
    generation: int = Field(ge=0)
    """Newest saved generation; 0 until the disk first publishes."""

    status: DiskStatus


class DevboxResponse(HttpModel):
    """How to reach a devbox and what it is doing, as the control plane sees it."""

    ssh_command: str
    """The CLI command that opens a session."""

    ssh_host: str
    """The host name `lazycloud ssh-config` writes, for editors that connect over SSH."""

    state: DevboxState
    phase: DevboxPhase
    phase_reason: str = ""
    """Why the last start failed, when `phase` is `failed`."""

    container_id: str | None = None
    """The container running or starting the devbox; null while it is stopped."""

    open_connections: int = Field(ge=0)
    """Connections held open through the pod's proxy, SSH sessions included."""

    idle_deadline: datetime | None = None
    """When the running container stops unless something connects.

    Null while a connection holds it open, while nothing runs, and once the
    deadline has passed and the next autoscaler pass may stop it.
    """

    disk: DevboxDiskResponse | None = None
    """The root disk; null until the devbox first starts and creates it."""


class DeploymentDetailResponse(DeploymentResponse):
    devbox: DevboxResponse | None = None
    """Present exactly when the deployment is a devbox."""


class DeploymentListResponse(HttpModel):
    data: list[DeploymentResponse] = Field(default_factory=list)
    next: str = ""


class DeploymentUrlResponse(HttpModel):
    deployment: DeploymentResponse
    stub: StubResponse | None = None
    url: str


class DeploymentPackageObjectResponse(HttpModel):
    id: str
    bucket: str
    key: str
    size: int
    sha256: str
    content_type: str = "application/octet-stream"
    created_at: datetime
    updated_at: datetime


class DeploymentPackagePlanResponse(HttpModel):
    workspace_id: str
    stub_id: str
    object: DeploymentPackageObjectResponse | None = None
    presigned_url: str | None = None
    expires_in_seconds: int = 600
    filename: str = "package"


class DeploymentStopAllResponse(HttpModel):
    stopped: list[DeploymentResponse] = Field(default_factory=list)


__all__ = [
    "DeploymentActionCapabilitiesResponse",
    "DeploymentDetailResponse",
    "DeploymentListResponse",
    "DeploymentPackageObjectResponse",
    "DeploymentPackagePlanResponse",
    "DeploymentResourcesResponse",
    "DeploymentResponse",
    "DeploymentScaleRequest",
    "DeploymentScalingResponse",
    "DeploymentSpecResponse",
    "DeploymentStopAllResponse",
    "DeploymentUrlResponse",
    "DevboxDiskResponse",
    "DevboxResponse",
]
