from __future__ import annotations

from datetime import date

from pydantic import Field, field_validator

from shared.contracts import ContractModel
from shared.enums import StringEnum
from shared.usage import UsageUnit


class BillableMetric(StringEnum):
    """What a billing line charges for.

    Declaration order is load-bearing: report line ordering derives from it, so a
    member inserted in the middle reorders every invoice.

    The `Managed*` members are the same compute measured on a customer's own
    cloud account. They are separate metrics rather than a flag on the originals
    because they answer a different question — not what the compute is worth, but
    what managing it is worth — and a report shows both at once when one app runs
    on both fleets.
    """

    CpuSeconds = "cpu_seconds"
    MemoryGibSeconds = "memory_gib_seconds"
    GpuSeconds = "gpu_seconds"
    ManagedCpuSeconds = "managed_cpu_seconds"
    ManagedMemoryGibSeconds = "managed_memory_gib_seconds"
    ManagedGpuSeconds = "managed_gpu_seconds"


PRICED_METRICS: tuple[BillableMetric, ...] = (
    BillableMetric.CpuSeconds,
    BillableMetric.MemoryGibSeconds,
    BillableMetric.GpuSeconds,
    BillableMetric.ManagedCpuSeconds,
    BillableMetric.ManagedMemoryGibSeconds,
    BillableMetric.ManagedGpuSeconds,
)
"""Every metric a price catalog must cover, and every one a report can charge.

All of these come from a container's allocation over a metering window, which is
why they accumulate per window rather than summing outright: one window can emit
both a duration record and directly recorded seconds covering the same interval,
and the directly recorded figure takes precedence so those seconds are charged
once. Neither figure is pure measurement — a container that bursts past its
request is billed the burst, and one that idles under it is billed the request.
"""


class BillingCoverageStatus(StringEnum):
    Empty = "empty"
    Complete = "complete"
    Partial = "partial"
    Unpriced = "unpriced"


class SellPriceConfig(ContractModel):
    """What LazyCloud charges for one metric.

    `price_per_unit_nanos` admits zero. A metered dimension we have chosen not to
    charge for is a real state—it keeps the meter, the ledger entry and the
    invoice line, and only the rate is zero—and it is not expressible if the
    contract demands a positive price.
    """

    metric: BillableMetric
    label: str
    unit: UsageUnit
    price_per_unit_nanos: int = Field(ge=0)
    currency: str
    effective_date: date
    variant: str = Field(default="", max_length=64)
    """Distinguishes rates sharing a metric—the GPU model, for `gpu_seconds`.

    Empty means the rate covers the whole metric.
    """
    note: str = ""

    @field_validator("label")
    @classmethod
    def _non_empty_label(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("sell price label cannot be empty")
        return normalized

    @field_validator("currency")
    @classmethod
    def _currency_code(cls, value: str) -> str:
        normalized = value.strip().upper()
        if len(normalized) != 3 or not normalized.isalpha():
            raise ValueError("sell price currency must be a three-letter code")
        return normalized


__all__ = [
    "PRICED_METRICS",
    "BillableMetric",
    "BillingCoverageStatus",
    "SellPriceConfig",
]
