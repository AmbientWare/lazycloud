from __future__ import annotations

from datetime import datetime

from pydantic import Field, JsonValue

from shared.app_identity import DEFAULT_RESOURCE_TYPE
from shared.http.base import HttpModel


class ConcurrencyLimitSetRequest(HttpModel):
    name: str
    limit: int
    workspace: str = "default"
    resource_type: str = DEFAULT_RESOURCE_TYPE
    resource_id: str | None = None
    metadata: dict[str, JsonValue] = Field(default_factory=dict)


class ConcurrencyLimitResponse(HttpModel):
    id: str
    workspace_id: str
    name: str
    limit: int
    in_flight: int = 0
    resource_type: str = DEFAULT_RESOURCE_TYPE
    resource_id: str | None = None
    metadata: dict[str, JsonValue] = Field(default_factory=dict)
    available: int
    saturated: bool
    created_at: datetime
    updated_at: datetime


class ConcurrencyLimitListResponse(HttpModel):
    limits: list[ConcurrencyLimitResponse] = Field(default_factory=list)


class ConcurrencyAcquireResponse(HttpModel):
    status: str
    acquired: bool
    record: ConcurrencyLimitResponse
    available_before: int
    available_after: int
    reason: str


__all__ = [
    "ConcurrencyAcquireResponse",
    "ConcurrencyLimitListResponse",
    "ConcurrencyLimitResponse",
    "ConcurrencyLimitSetRequest",
]
