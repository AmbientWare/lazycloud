from __future__ import annotations

from datetime import datetime

from pydantic import Field, JsonValue

from shared.billing_quotes import BilledDimension, LedgerComponent
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


class UsageCostBucket(StringEnum):
    """How wide one interval of a cost series is.

    Closed, and the two widths a spend chart is read at: a day for a billing
    period, an hour for the last day of it. Anything finer would draw a bar per
    metering window, and anything coarser is a total rather than a shape.
    """

    Hour = "hour"
    Day = "day"


class UsageCostComponentResponse(HttpModel):
    """One resource's share of a row, and the invoice line it rolls up into.

    Present for every component the row produced usage in, including the ones
    priced at zero — an egress line reading $0.00 is how a customer sees that
    their traffic is measured and free rather than unmeasured.

    `component` says which resource `quantity` counts and therefore which unit
    it is in. `dimension` names the invoice line that component rolls up into,
    so a breakdown and a bill can be read against each other without the browser
    holding its own map of which resource is billed under what.
    """

    dimension: BilledDimension
    component: LedgerComponent
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
    workspace_id: str = ""
    workspace_name: str = ""
    """Workspace this row's cost was incurred in.

    Carried on the row rather than the envelope because the envelope may cover
    an account, where every row has a different answer. Grouped on, not derived
    from the app: usage that reached no app carries an empty `app_id`, and that
    same empty key in two workspaces would otherwise sum into one row naming
    neither.
    """

    workload_id: str = ""
    workload_name: str = ""
    workload_kind: str = ""
    task_id: str = ""
    cost_nanos: int = Field(default=0, ge=0)
    components: list[UsageCostComponentResponse] = Field(default_factory=list)


class UsageCostListResponse(HttpModel):
    """A page of cost, ordered by what cost the most.

    `cost_nanos` totals the whole window rather than the page, so a page that
    does not sum to it is a page with more behind it, and the figure a customer
    is shown as their bill never depends on how far they scrolled.

    `workspace_id` names the one workspace this page covers, and is empty where
    the page covers an account: an account is invoiced as a whole, and picking
    one of its workspaces to name would be a page labelled with a scope it does
    not have.
    """

    workspace_id: str = ""
    start: datetime
    end: datetime
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    group_by: UsageCostGroupKey
    cost_nanos: int = Field(default=0, ge=0)
    data: list[UsageCostRowResponse] = Field(default_factory=list)
    next: str = ""


class UsageCostDimensionTotalResponse(HttpModel):
    """One invoice line's share of an interval.

    Keyed by dimension rather than by component because a bar is read against a
    bill: the components a dimension breaks into are this platform's own
    granularity, and they are already carried per row by the breakdown beside
    the chart.

    A dimension nothing was metered in is absent rather than zero. On a cost row
    a zero line says the resource was measured and free; over an interval no row
    was written at all, and stating zero would claim a measurement that was never
    taken.
    """

    dimension: BilledDimension
    cost_nanos: int = Field(default=0, ge=0)


class UsageCostBucketResponse(HttpModel):
    """What one interval of the window cost.

    Every interval the window covers is present, including the ones that cost
    nothing: a chart that skips them draws a run of quiet days as a run of busy
    ones side by side.

    `ended_at` is clipped to the end of the window, so the last interval reports
    the span it actually covers rather than one running past the question asked.
    """

    started_at: datetime
    ended_at: datetime
    cost_nanos: int = Field(default=0, ge=0)
    dimensions: list[UsageCostDimensionTotalResponse] = Field(default_factory=list)


class UsageCostSeriesResponse(HttpModel):
    """An account's spend over a window, as the shape it took.

    Intervals are measured from `start` in whole `bucket` widths rather than
    truncated to a calendar, so a window that opens on a UTC boundary is read in
    UTC days and one that does not is still read in exact days from where it
    opened. `cost_nanos` is the intervals summed, so the total a customer reads
    and the bars they read it from cannot state different figures.
    """

    start: datetime
    end: datetime
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    bucket: UsageCostBucket
    cost_nanos: int = Field(default=0, ge=0)
    data: list[UsageCostBucketResponse] = Field(default_factory=list)


__all__ = [
    "UsageAggregationResponse",
    "UsageCostBucket",
    "UsageCostBucketResponse",
    "UsageCostComponentResponse",
    "UsageCostDimensionTotalResponse",
    "UsageCostGroupKey",
    "UsageCostListResponse",
    "UsageCostRowResponse",
    "UsageCostSeriesResponse",
    "UsageRecordListResponse",
    "UsageRecordResponse",
    "UsageSummaryResponse",
]
