from __future__ import annotations

from datetime import date

from pydantic import ConfigDict, Field

from shared.billing import BillableMetric
from shared.contracts import ContractModel
from shared.usage import UsageUnit


class BillingLedgerEntry(ContractModel):
    """What one workspace owed for one metric on one day, as last computed.

    Working state, not a frozen record. A day is recomputed whenever its usage
    changes — late arrivals, a retried run — and recomputation prices at the
    catalog in force when it runs, so a rate change moves what an open day costs.

    Freezing is the closing period's job, not this table's. Until a period
    closes, what a day cost is still a question with a current answer; after it
    closes, it is a number somebody was charged. Anything that needs the second
    reads the period, not this.
    """

    model_config = ConfigDict(frozen=True)

    workspace_id: str
    user_id: str
    day: date
    metric: BillableMetric
    variant: str = Field(default="", max_length=64)
    """Which rate produced the line — the GPU model, or empty where one rate
    covers the whole metric. Part of the identity, because nine GPU models share
    one metric and differ by an order of magnitude in price."""

    effective_date: date | None = None
    """When the rate that priced this line took effect. Part of the line's
    identity: a rate change during a day splits it in two."""

    quantity: float = Field(ge=0)
    unit: UsageUnit
    price_per_unit_nanos: int | None = Field(default=None, ge=0)
    """Null where nothing priced this line, which is not the same as free: an
    unpriced GPU model has to stay visible as a gap, while a rate of zero is a
    dimension we metered and chose not to charge for."""

    cost_nanos: int = Field(ge=0)
    """Required, so "nobody computed this" cannot arrive looking like "free"."""

    currency: str = Field(min_length=3, max_length=3)


__all__ = ["BillingLedgerEntry"]
