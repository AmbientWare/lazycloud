from datetime import datetime

from pydantic import BaseModel, Field


class PodMetrics(BaseModel):
    cpu_cores: float = Field(default=0.0)
    memory_gb: float = Field(default=0.0)


class PodUsage(BaseModel):
    pod: str
    service: str
    cpu_core_seconds: float = Field(default=0.0)
    memory_gb_seconds: float = Field(default=0.0)


class ServiceUsage(BaseModel):
    cpu_core_seconds: float = Field(default=0.0)
    memory_gb_seconds: float = Field(default=0.0)
    pod_count: int = Field(default=0)


class UsageTotals(BaseModel):
    cpu_core_seconds: float = Field(default=0.0)
    memory_gb_seconds: float = Field(default=0.0)
    storage_gb_hours: float = Field(default=0.0)


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
    by_service: dict[str, ServiceUsage]
    by_pod: list[PodUsage]
