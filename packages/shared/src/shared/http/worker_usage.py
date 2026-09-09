from pydantic import AwareDatetime, Field

from shared.http.base import HttpModel
from shared.usage import UsageRecord


class WorkerUsageWindowRequest(HttpModel):
    container_id: str = Field(min_length=1)
    started_at: AwareDatetime
    ended_at: AwareDatetime
    records: tuple[UsageRecord, ...] = Field(min_length=1, max_length=32)
    measurement_complete: bool


class WorkerUsageWindowResponse(HttpModel):
    records: tuple[UsageRecord, ...]
