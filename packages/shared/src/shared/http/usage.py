from __future__ import annotations

from datetime import datetime

from pydantic import Field, JsonValue

from shared.billing import BillableMetric, BillingCostBasis
from shared.enums import StringEnum
from shared.http.base import HttpModel
from shared.usage import UsageMetric, UsageUnit


class UsageBillingPeriod(StringEnum):
    Current = "current"


class UsageRecordResponse(HttpModel):
    id: str
    workspace_id: str
    resource_type: str
    resource_id: str
    metric: UsageMetric
    quantity: float
    unit: UsageUnit
    labels: dict[str, str] = Field(default_factory=dict)
    metadata: dict[str, JsonValue] = Field(default_factory=dict)
    created_at: datetime


class UsageRecordListResponse(HttpModel):
    data: list[UsageRecordResponse] = Field(default_factory=list)
    next: str = ""


class UsageAggregationResponse(HttpModel):
    workspace_id: str
    metric: UsageMetric
    quantity: float
    unit: UsageUnit
    labels: dict[str, str] = Field(default_factory=dict)


class UsageSummaryResponse(HttpModel):
    rows: list[UsageAggregationResponse] = Field(default_factory=list)


class UsageBillingLineResponse(HttpModel):
    metric: BillableMetric
    label: str
    quantity: float
    unit: UsageUnit
    price_per_unit_nanos: int | None = None
    cost_nanos: int
    cost_basis: BillingCostBasis


class UsageBillingAttributionResponse(HttpModel):
    app_id: str = ""
    app_name: str = "Unlinked"
    workload_id: str = ""
    workload_name: str = ""
    workload_kind: str = ""
    tasks: int = 0
    total_cost_nanos: int = 0
    lines: list[UsageBillingLineResponse] = Field(default_factory=list)


class UsageBillingAppSummaryResponse(HttpModel):
    app_id: str = ""
    app_name: str = "Unlinked"
    tasks: int = 0
    total_cost_nanos: int = 0
    lines: list[UsageBillingLineResponse] = Field(default_factory=list)


class UsageBillingBucketResponse(HttpModel):
    start: datetime
    end: datetime
    total_cost_nanos: int = 0
    lines: list[UsageBillingLineResponse] = Field(default_factory=list)


class UsageBillingOverviewResponse(HttpModel):
    workspace_id: str
    start: datetime
    end: datetime
    currency: str
    total_cost_nanos: int = 0
    contains_estimates: bool = True
    summary: list[UsageBillingLineResponse] = Field(default_factory=list)
    apps: list[UsageBillingAppSummaryResponse] = Field(default_factory=list)
    activity: list[UsageBillingBucketResponse] = Field(default_factory=list)


class UsageBillingWorkloadListResponse(HttpModel):
    workspace_id: str
    app_id: str
    start: datetime
    end: datetime
    currency: str
    data: list[UsageBillingAttributionResponse] = Field(default_factory=list)


__all__ = [
    "UsageAggregationResponse",
    "UsageBillingAppSummaryResponse",
    "UsageBillingAttributionResponse",
    "UsageBillingBucketResponse",
    "UsageBillingLineResponse",
    "UsageBillingOverviewResponse",
    "UsageBillingPeriod",
    "UsageBillingWorkloadListResponse",
    "UsageRecordListResponse",
    "UsageRecordResponse",
    "UsageSummaryResponse",
]
