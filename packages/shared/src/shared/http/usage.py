from __future__ import annotations

from datetime import datetime

from pydantic import Field, JsonValue

from shared.billing_quotes import BilledDimension, LedgerComponent, QuotedUnit
from shared.enums import StringEnum
from shared.http.base import HttpModel
from shared.usage import UsageMetric, UsageUnit


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


class UsageCostGroupKey(StringEnum):
    """What one row of a cost breakdown stands for.

    Closed, and the three levels of the product model that carry a cost. A
    container is where the cost is measured, not where anybody reads it: nobody
    asks what one container of a queue worker cost, they ask what the queue cost.
    """

    App = "app"
    Workload = "workload"
    Task = "task"


class UsageCostComponentResponse(HttpModel):
    """One resource's share of a row, in the unit its rate is published per.

    Present for every component the row produced usage in, including the ones
    priced at zero — an egress line reading $0.00 is how a customer sees that
    their traffic is measured and free rather than unmeasured.

    `dimension` names the invoice line the component rolls up into, so a
    breakdown and a bill can be read against each other without the browser
    holding its own map of which resource is billed under what.
    """

    dimension: BilledDimension
    component: LedgerComponent
    unit: QuotedUnit
    quantity: float
    cost_nanos: int = Field(ge=0)


class UsageCostRowResponse(HttpModel):
    """What one app, workload or task cost over the window.

    Every level carries the ids above it, so a row identifies itself without the
    caller remembering what it asked to group by. An empty id is real: usage that
    reached no app is not usage belonging to an app named "".

    A name is empty where the app or workload has since been deleted. The cost
    stays — it was incurred — and the row says so by carrying an id and no name
    rather than a label invented to fill the column.

    What the row was charged for is in `components` and nowhere else. A copy of
    one component's quantity hoisted into a field of its own would be the same
    ledger rows summed twice, and the two sums are what a customer would be shown
    disagreeing.
    """

    app_id: str = ""
    app_name: str = ""
    workload_id: str = ""
    workload_name: str = ""
    workload_kind: str = ""
    task_id: str = ""
    cost_nanos: int = Field(default=0, ge=0)
    components: list[UsageCostComponentResponse] = Field(default_factory=list)


class UsageCostListResponse(HttpModel):
    """A page of a workspace's cost, ordered by what cost the most.

    `cost_nanos` totals the whole window rather than the page, so a page that
    does not sum to it is a page with more behind it, and the figure a customer
    is shown as their bill never depends on how far they scrolled.
    """

    workspace_id: str
    start: datetime
    end: datetime
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    group_by: UsageCostGroupKey
    cost_nanos: int = Field(default=0, ge=0)
    data: list[UsageCostRowResponse] = Field(default_factory=list)
    next: str = ""


__all__ = [
    "UsageAggregationResponse",
    "UsageCostComponentResponse",
    "UsageCostGroupKey",
    "UsageCostListResponse",
    "UsageCostRowResponse",
    "UsageRecordListResponse",
    "UsageRecordResponse",
    "UsageSummaryResponse",
]
