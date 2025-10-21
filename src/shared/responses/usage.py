from datetime import datetime

from pydantic import BaseModel


class CurrentUsageData(BaseModel):
    cpu_cores: float
    memory_gb: float
    storage_gb: float


class CurrentUsageResponse(BaseModel):
    workspace_id: str
    namespace: str
    timestamp: datetime
    current_usage: CurrentUsageData


class UsagePeriodInfo(BaseModel):
    start: datetime
    end: datetime


class UsageMetrics(BaseModel):
    cpu_core_hours: float
    memory_gb_hours: float
    storage_gb_hours: float


class WorkspaceUsageResponse(BaseModel):
    workspace_id: str
    period: UsagePeriodInfo
    usage: UsageMetrics
    record_count: int


class ServiceBreakdownItem(BaseModel):
    cpu_core_hours: float
    memory_gb_hours: float
    pod_count: int


class WorkspaceUsageBreakdownResponse(BaseModel):
    workspace_id: str
    period: UsagePeriodInfo
    usage: UsageMetrics
    by_service: dict[str, ServiceBreakdownItem]
