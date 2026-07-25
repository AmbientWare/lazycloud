from __future__ import annotations

from datetime import date
from urllib.parse import urlparse

from pydantic import field_validator

from shared.contracts import ContractModel
from shared.enums import StringEnum
from shared.usage import UsageUnit


class BillableMetric(StringEnum):
    CpuSeconds = "cpu_seconds"
    MemoryGibSeconds = "memory_gib_seconds"
    GpuSeconds = "gpu_seconds"
    RecordedCompute = "recorded_compute"
    ManagedCompute = "managed_compute_reservation_seconds"
    CustomerCloudManagement = "customer_cloud_management_seconds"


COMPUTE_PRICE_METRICS: tuple[BillableMetric, ...] = (
    BillableMetric.CpuSeconds,
    BillableMetric.MemoryGibSeconds,
    BillableMetric.GpuSeconds,
)


class BillingCostBasis(StringEnum):
    CatalogEstimate = "catalog_estimate"
    RecordedAllocation = "recorded_allocation"
    Recorded = "recorded"


class BillingCoverageStatus(StringEnum):
    Empty = "empty"
    Complete = "complete"
    Partial = "partial"
    Unpriced = "unpriced"


class UsagePriceConfig(ContractModel):
    metric: BillableMetric
    label: str
    unit: UsageUnit
    price_per_unit_nanos: int
    currency: str
    provider: str
    service: str
    region: str
    effective_date: date
    source_url: str
    note: str = ""

    @field_validator("label", "provider", "service", "region")
    @classmethod
    def _non_empty_text(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("usage price catalog text fields cannot be empty")
        return normalized

    @field_validator("currency")
    @classmethod
    def _currency_code(cls, value: str) -> str:
        normalized = value.strip().upper()
        if len(normalized) != 3 or not normalized.isalpha():
            raise ValueError("usage price currency must be a three-letter code")
        return normalized

    @field_validator("price_per_unit_nanos")
    @classmethod
    def _positive_price(cls, value: int) -> int:
        if value <= 0:
            raise ValueError("usage price must be positive")
        return value

    @field_validator("source_url")
    @classmethod
    def _absolute_source_url(cls, value: str) -> str:
        normalized = value.strip()
        parsed = urlparse(normalized)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("usage price source URL must be absolute HTTP(S)")
        return normalized


__all__ = [
    "COMPUTE_PRICE_METRICS",
    "BillableMetric",
    "BillingCostBasis",
    "BillingCoverageStatus",
    "UsagePriceConfig",
]
