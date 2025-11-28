from datetime import datetime

from pydantic import BaseModel, Field


class PodMetrics(BaseModel):
    cpu_cores: float = Field(default=0.0)
    memory_gb: float = Field(default=0.0)


class PodUsage(BaseModel):
    pod: str
    service: str
    release_name: str | None = None
    cpu_core_seconds: float = Field(default=0.0)
    memory_gb_seconds: float = Field(default=0.0)


class StorageUsage(BaseModel):
    pvc_name: str
    storage_class: str
    gb_hours: float = Field(default=0.0)


class UsageTotals(BaseModel):
    cpu_core_seconds: float = Field(default=0.0)
    memory_gb_seconds: float = Field(default=0.0)
    storage_gb_hours: float = Field(default=0.0)
    standard_gb_hours: float = Field(default=0.0)
    shared_gb_hours: float = Field(default=0.0)


class UsagePeriod(BaseModel):
    start: datetime
    end: datetime
    duration_seconds: float


class NamespaceSummary(BaseModel):
    namespace: str
    timestamp: datetime
    cpu_cores: float = Field(default=0.0)
    memory_gb: float = Field(default=0.0)
    storage_gb: float = Field(default=0.0)


class NamespaceBreakdown(BaseModel):
    namespace: str
    period: UsagePeriod
    totals: UsageTotals
    by_pod: list[PodUsage]
    by_pvc: list[StorageUsage] = Field(default_factory=list)
