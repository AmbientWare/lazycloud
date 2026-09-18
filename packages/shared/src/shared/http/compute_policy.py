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
from shared.deployments import DeploymentKind
from shared.http.base import HttpModel


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
    hourly_micros: int | None = Field(default=None, ge=0)
    daily_micros: int | None = Field(default=None, ge=0)
    currency: Literal["USD"] = "USD"
    estimated: bool = True


class WorkspaceComputeSummaryResponse(HttpModel):
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
    machine: str = ""
    cpu_millicores: int = Field(default=0, ge=0)
    memory_mb: int = Field(default=0, ge=0)
    gpu: list[str] = Field(default_factory=list)
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
    "WorkspaceComputeInstanceListResponse",
    "WorkspaceComputeInstanceResponse",
    "WorkspaceComputeSummaryResponse",
    "WorkspaceComputeWorkloadListResponse",
    "WorkspaceComputeWorkloadResponse",
]
