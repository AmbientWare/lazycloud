from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import Field

from shared.aws_connections import AwsAccountConnectionPhase
from shared.compute_enrollment import (
    MachineBootstrapFailureReason,
    MachineBootstrapPhase,
    MachineServiceState,
)
from shared.compute_policy import (
    AwsWorkspaceComputePolicy,
)
from shared.deployments import DeploymentKind
from shared.http.base import HttpModel


class WorkspaceComputePolicyUpdateRequest(HttpModel):
    expected_revision: int = Field(ge=1)
    default_pool: str = Field(min_length=1, max_length=240)
    aws: AwsWorkspaceComputePolicy


class AwsWorkspaceComputePolicyPatch(HttpModel):
    """Fields a caller chose to change. Omitted is not the same as zero."""

    default_region: str | None = None
    default_instance_type: str | None = None
    initial_cpu_workers: int | None = None
    min_cpu_workers: int | None = None
    max_cpu_instances: int | None = None
    max_gpu_instances: int | None = None
    min_free_cpu_millicores: int | None = None
    min_free_memory_mib: int | None = None
    allowed_regions: tuple[str, ...] | None = None
    allowed_instance_types: tuple[str, ...] | None = None
    idle_timeout_seconds: int | None = None
    root_volume_gib: int | None = None


class WorkspaceComputePolicyPatchRequest(HttpModel):
    expected_revision: int = Field(ge=1)
    default_pool: str | None = None
    aws: AwsWorkspaceComputePolicyPatch = Field(default_factory=AwsWorkspaceComputePolicyPatch)


class WorkspaceComputePolicyResponse(HttpModel):
    revision: int = Field(ge=1)
    default_pool: str
    aws: AwsWorkspaceComputePolicy
    created_at: datetime
    updated_at: datetime


class MachinePoolResponse(HttpModel):
    """One pool a workload may name, and what feeds it."""

    name: str
    is_default: bool = False
    providers: tuple[str, ...] = ()
    """Distinct providers behind this pool, e.g. `aws`, `agent`, `local`."""
    unit_count: int = Field(default=0, ge=0)
    gpu_types: tuple[str, ...] = ()
    """GPU types this pool can host, empty when it hosts CPU workloads only."""


class MachinePoolListResponse(HttpModel):
    data: list[MachinePoolResponse] = Field(default_factory=list)
    next: str = ""


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
    service_state: MachineServiceState
    bootstrap_failure_reason: MachineBootstrapFailureReason | None = None
    bootstrap_failure_detail: str = ""
    bootstrap_observed_at: datetime
    launch_attempt: int = Field(default=1, ge=1)
    booted_template_version: str = ""
    """Provider launch-configuration version the node booted with.

    Empty when the provider reports none. A pool rolls its configuration
    forward without disturbing running nodes, so nodes of the same pool
    legitimately differ here, and this is what says which release each one
    is on.
    """
    created_at: datetime


class WorkspaceComputeInstanceListResponse(HttpModel):
    data: list[WorkspaceComputeInstanceResponse] = Field(default_factory=list)
    next: str = ""


class WorkspaceComputeWorkloadResponse(HttpModel):
    deployment_id: str
    app_id: str | None = None
    name: str
    kind: DeploymentKind
    pool: str
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
    "MachinePoolListResponse",
    "MachinePoolResponse",
    "WorkspaceComputeInstanceListResponse",
    "WorkspaceComputeInstanceResponse",
    "WorkspaceComputePolicyResponse",
    "WorkspaceComputePolicyUpdateRequest",
    "WorkspaceComputeSummaryResponse",
    "WorkspaceComputeWorkloadListResponse",
    "WorkspaceComputeWorkloadResponse",
]
