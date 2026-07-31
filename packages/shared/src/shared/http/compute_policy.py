from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import Field

from shared.aws_connections import AwsAccountConnectionPhase
from shared.compute_enrollment import (
    MachineBootstrapFailureReason,
    MachineBootstrapPhase,
)
from shared.compute_policy import (
    AwsWorkspaceComputePolicy,
    ComputePlacementSource,
    ComputePlacementTarget,
)
from shared.deployments import DeploymentKind
from shared.http.base import HttpModel


class WorkspaceComputePolicyUpdateRequest(HttpModel):
    expected_revision: int = Field(ge=1)
    default_placement: ComputePlacementTarget
    aws: AwsWorkspaceComputePolicy


class WorkspaceComputePolicyResponse(HttpModel):
    revision: int = Field(ge=1)
    default_placement: ComputePlacementTarget
    aws: AwsWorkspaceComputePolicy
    created_at: datetime
    updated_at: datetime


class ResolvedComputePlacementResponse(HttpModel):
    target: ComputePlacementTarget
    source: ComputePlacementSource
    provider: str
    region: str


class ComputeCatalogInstanceResponse(HttpModel):
    instance_type: str
    kind: str
    cpu_millicores: int = Field(gt=0)
    memory_mb: int = Field(gt=0)
    gpu: str | None = None
    gpu_count: int = Field(default=0, ge=0)


class ComputeCatalogRegionResponse(HttpModel):
    provider: str
    region: str
    instances: list[ComputeCatalogInstanceResponse] = Field(default_factory=list)


class ComputeCatalogResponse(HttpModel):
    data: list[ComputeCatalogRegionResponse] = Field(default_factory=list)
    next: str = ""


class ComputeConnectionSummaryResponse(HttpModel):
    account_id: str
    phase: AwsAccountConnectionPhase


class ComputeCapacitySummaryResponse(HttpModel):
    total: int = Field(default=0, ge=0)
    ready: int = Field(default=0, ge=0)
    pending: int = Field(default=0, ge=0)
    degraded: int = Field(default=0, ge=0)


class ComputeCostSummaryResponse(HttpModel):
    hourly_micros: int = Field(default=0, ge=0)
    daily_micros: int = Field(default=0, ge=0)
    currency: Literal["USD"] = "USD"
    estimated: bool = True


class WorkspaceComputeSummaryResponse(HttpModel):
    policy: WorkspaceComputePolicyResponse
    connection: ComputeConnectionSummaryResponse | None = None
    instances: ComputeCapacitySummaryResponse = Field(
        default_factory=ComputeCapacitySummaryResponse
    )
    cost: ComputeCostSummaryResponse = Field(default_factory=ComputeCostSummaryResponse)
    workload_count: int = Field(default=0, ge=0)


class WorkspaceComputeInstanceResponse(HttpModel):
    id: str
    machine_id: str | None = None
    provider: str
    region: str
    instance_type: str | None = None
    status: str
    gpu: str | None = None
    gpu_count: int = Field(default=0, ge=0)
    cpu_millicores: int = Field(default=0, ge=0)
    memory_mb: int = Field(default=0, ge=0)
    bootstrap_phase: MachineBootstrapPhase
    bootstrap_failure_reason: MachineBootstrapFailureReason | None = None
    bootstrap_failure_detail: str = ""
    bootstrap_observed_at: datetime
    launch_attempt: int = Field(default=1, ge=1)
    created_at: datetime


class WorkspaceComputeInstanceListResponse(HttpModel):
    data: list[WorkspaceComputeInstanceResponse] = Field(default_factory=list)
    next: str = ""


class WorkspaceComputeWorkloadResponse(HttpModel):
    deployment_id: str
    app_id: str | None = None
    name: str
    kind: DeploymentKind
    placement: ResolvedComputePlacementResponse
    cpu_millicores: int = Field(default=0, ge=0)
    memory_mb: int = Field(default=0, ge=0)
    gpu: str | None = None
    gpu_count: int = Field(default=0, ge=0)


class WorkspaceComputeWorkloadListResponse(HttpModel):
    data: list[WorkspaceComputeWorkloadResponse] = Field(default_factory=list)
    next: str = ""


__all__ = [
    "ComputeCapacitySummaryResponse",
    "ComputeCatalogInstanceResponse",
    "ComputeCatalogRegionResponse",
    "ComputeCatalogResponse",
    "ComputeConnectionSummaryResponse",
    "ComputeCostSummaryResponse",
    "ResolvedComputePlacementResponse",
    "WorkspaceComputeInstanceListResponse",
    "WorkspaceComputeInstanceResponse",
    "WorkspaceComputePolicyResponse",
    "WorkspaceComputePolicyUpdateRequest",
    "WorkspaceComputeSummaryResponse",
    "WorkspaceComputeWorkloadListResponse",
    "WorkspaceComputeWorkloadResponse",
]
