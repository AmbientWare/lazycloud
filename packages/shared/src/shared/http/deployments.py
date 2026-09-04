from __future__ import annotations

from datetime import datetime

from pydantic import Field

from shared.compute_policy import MachinePool
from shared.deployment_records import CpuRequest, MemoryRequest
from shared.deployments import DeploymentKind
from shared.http.base import HttpModel
from shared.http.stubs import StubResponse
from shared.placement import ProductRegion


class DeploymentResourcesResponse(HttpModel):
    region: ProductRegion | None = Field(default=None, exclude_if=lambda value: value is None)
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
    pool: MachinePool = MachinePool("")


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
]
