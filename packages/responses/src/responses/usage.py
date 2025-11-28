from datetime import datetime
from typing import Literal

from models.billing import STORAGE_CLASS_EBS, STORAGE_CLASS_EFS
from pydantic import BaseModel


class UsagePeriodInfo(BaseModel):
    start: datetime
    end: datetime


class UsageMetrics(BaseModel):
    cpu_core_hours: float
    memory_gb_hours: float
    standard_gb_hours: float
    shared_gb_hours: float
    build_minutes: float
    public_endpoint_hours: float
    costs: "MeterCostBreakdown | None" = None


class ServiceUsageItem(BaseModel):
    """Internal type for service usage processing"""

    service_name: str
    cpu_core_seconds: float
    memory_gb_seconds: float


class VolumeUsageItem(BaseModel):
    """Internal type for volume usage processing"""

    volume_name: str
    storage_class: Literal[STORAGE_CLASS_EBS, STORAGE_CLASS_EFS]
    gb_hours: float


class DailyUsageData(BaseModel):
    date: str
    cpu_core_hours: float
    memory_gb_hours: float
    standard_gb_hours: float
    shared_gb_hours: float
    build_minutes: float
    public_endpoint_hours: float
    costs: "MeterCostBreakdown | None" = None


class WorkspaceUsageSummary(BaseModel):
    """Workspace-level usage summary with minimal deployment info (no usage metrics)"""

    workspace_id: str
    workspace_name: str
    workspace_status: Literal["Active", "Inactive"]
    usage: UsageMetrics
    record_count: int
    deployments: list["DeploymentUsageOverview"]


class AggregatedUsageResponse(BaseModel):
    """Aggregated usage across all user workspaces"""

    period: UsagePeriodInfo
    usage: UsageMetrics
    workspace_count: int
    record_count: int
    workspaces: list[WorkspaceUsageSummary]


class AggregatedDailyUsageResponse(BaseModel):
    """Aggregated daily usage across all user workspaces"""

    period: UsagePeriodInfo
    daily_usage: list[DailyUsageData]
    workspace_count: int


# Cost breakdown models


class MeterCostBreakdown(BaseModel):
    """Cost breakdown by meter type (estimated from cached prices)."""

    cpu_cost: float
    memory_cost: float
    standard_cost: float
    shared_cost: float
    build_cost: float
    endpoint_cost: float
    total_cost: float


class ServiceCostBreakdown(BaseModel):
    """Cost breakdown for a single service (estimated)."""

    service_name: str
    cpu_core_hours: float | None = None
    memory_gb_hours: float | None = None
    cpu_cost: float
    memory_cost: float
    total_compute_cost: float
    percentage_of_total: float


class VolumeCostBreakdown(BaseModel):
    """Cost breakdown for a single volume (estimated)."""

    volume_name: str
    storage_class: Literal[STORAGE_CLASS_EBS, STORAGE_CLASS_EFS]
    storage_cost: float
    percentage_of_total: float


class WorkspaceCostBreakdownResponse(BaseModel):
    """Complete cost breakdown for workspace usage"""

    workspace_id: str
    period: UsagePeriodInfo
    meter_breakdown: MeterCostBreakdown
    service_breakdown: list[ServiceCostBreakdown]
    volume_breakdown: list[VolumeCostBreakdown]
    is_estimated: bool = True


class DeploymentUsageOverview(BaseModel):
    """Usage overview for a single deployment (summary only, no detailed breakdown)"""

    deployment_id: str
    deployment_name: str
    usage: UsageMetrics
    status: Literal["Active", "Inactive"]
    deployed_at: datetime | None = None
    deleted_at: datetime | None = None
