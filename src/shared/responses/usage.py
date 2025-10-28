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


class ServiceUsageItem(BaseModel):
    service_name: str
    cpu_core_seconds: float
    memory_gb_seconds: float


class VolumeUsageItem(BaseModel):
    """Individual volume usage"""

    volume_name: str
    storage_class: Literal[STORAGE_CLASS_S3, STORAGE_CLASS_EFS]
    gb_hours: float


class WorkspaceUsageResponse(BaseModel):
    workspace_id: str
    period: UsagePeriodInfo
    usage: UsageMetrics
    record_count: int
    deployment_id: str | None = None
    deployment_name: str | None = None
    services: list[ServiceUsageItem] | None = None
    volumes: list[VolumeUsageItem] | None = None


class DailyUsageData(BaseModel):
    date: str
    cpu_core_hours: float
    memory_gb_hours: float
    s3_gb_hours: float
    efs_gb_hours: float


class DailyUsageResponse(BaseModel):
    workspace_id: str
    period: UsagePeriodInfo
    daily_usage: list[DailyUsageData]
