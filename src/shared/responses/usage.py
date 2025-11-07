from datetime import datetime
from typing import Literal

from pydantic import BaseModel

from shared.models.billing import STORAGE_CLASS_EFS, STORAGE_CLASS_S3


class UsagePeriodInfo(BaseModel):
    start: datetime
    end: datetime


class UsageMetrics(BaseModel):
    cpu_core_hours: float
    memory_gb_hours: float
    s3_gb_hours: float
    efs_gb_hours: float
    costs: "MeterCostBreakdown | None" = None


class ServiceUsageItem(BaseModel):
    """Internal type for service usage processing"""

    service_name: str
    cpu_core_seconds: float
    memory_gb_seconds: float


class VolumeUsageItem(BaseModel):
    """Internal type for volume usage processing"""

    volume_name: str
    storage_class: Literal[STORAGE_CLASS_S3, STORAGE_CLASS_EFS]
    gb_hours: float


class DailyUsageData(BaseModel):
    date: str
    cpu_core_hours: float
    memory_gb_hours: float
    s3_gb_hours: float
    efs_gb_hours: float
    costs: "MeterCostBreakdown | None" = None


class AggregatedUsageResponse(BaseModel):
    """Aggregated usage across all user workspaces"""

    period: UsagePeriodInfo
    usage: UsageMetrics
    workspace_count: int
    record_count: int


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
    s3_cost: float
    efs_cost: float
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
    storage_class: Literal[STORAGE_CLASS_S3, STORAGE_CLASS_EFS]
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


class DeploymentUsageBreakdown(BaseModel):
    """Usage and cost breakdown for a single deployment (with detailed service/volume breakdown)"""

    deployment_id: str
    deployment_name: str
    usage: UsageMetrics
    cost_breakdown: WorkspaceCostBreakdownResponse


class WorkspaceUsageWithDeploymentsResponse(BaseModel):
    """Workspace usage with deployment overviews in one response"""

    workspace_id: str
    period: UsagePeriodInfo
    workspace_usage: UsageMetrics
    record_count: int
    deployments: list[DeploymentUsageOverview]
    workspace_status: Literal["Active", "Inactive"]
