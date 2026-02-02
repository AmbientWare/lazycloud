from datetime import date, datetime

from models.usage import DailyUsageStatus
from pydantic import BaseModel

from backend.database.models.base import (
    BaseDbModel,
    UUIDStr,
)


class DailyUsageRecord(BaseModel):
    workspace_id: UUIDStr
    usage_date: date
    status: DailyUsageStatus
    cpu_core_seconds: float
    memory_gb_seconds: float
    storage_gb_months: float
    build_minutes: float
    public_endpoint_hours: float
    intervals_collected: int
    expected_intervals: int
    billing_id: str | None = None
    billed_at: datetime | None = None
    billing_attempts: int = 0
    last_billing_error: str | None = None
    last_billing_attempt_at: datetime | None = None


class DailyUsageRecordInDb(DailyUsageRecord, BaseDbModel):
    """Pydantic model for a daily usage record that is stored in the database"""

    ...


class CollectedInterval(BaseModel):
    workspace_id: UUIDStr
    interval_start: datetime


class CollectedIntervalInDb(CollectedInterval, BaseDbModel):
    """Pydantic model for a collected interval that is stored in the database"""

    ...


class UsageBreakdownEvent(BaseModel):
    workspace_id: UUIDStr
    deployment_id: UUIDStr | None = None
    interval_start: datetime
    interval_end: datetime
    breakdown_type: str
    resource_name: str
    service_name: str | None = None
    storage_class: str | None = None
    cpu_core_seconds: float = 0.0
    memory_gb_seconds: float = 0.0
    gb_hours: float = 0.0
    endpoint_hours: float = 0.0
    build_minutes: float = 0.0


class UsageBreakdownEventInDb(UsageBreakdownEvent, BaseDbModel):
    """Pydantic model for a usage breakdown event that is stored in the database"""

    ...
