from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import ConfigDict, Field, model_validator

from shared.contracts import ContractModel
from shared.enums import StringEnum


class SupplierCpuUnit(StringEnum):
    Unknown = "unknown"
    Vcpu = "vcpu"
    PhysicalCore = "physical_core"


class SupplierNetworkAllowancePeriod(StringEnum):
    CalendarMonth = "calendar_month"


class SupplierNetworkAllowanceScope(StringEnum):
    Node = "node"
    Account = "account"


class SupplierNetworkTerms(ContractModel):
    """USD transfer prices per decimal GB. None means the supplier term is unknown."""

    model_config = ConfigDict(frozen=True)

    ingress_micros_per_gb: int | None = Field(default=None, ge=0)
    egress_micros_per_gb: int | None = Field(default=None, ge=0)
    included_egress_bytes: int | None = Field(default=None, ge=0)
    allowance_period: SupplierNetworkAllowancePeriod | None = None
    allowance_scope: SupplierNetworkAllowanceScope | None = None
    billing_quantum_bytes: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def validate_allowance(self) -> SupplierNetworkTerms:
        if self.included_egress_bytes is not None and self.included_egress_bytes > 0:
            if self.allowance_period is None or self.allowance_scope is None:
                raise ValueError("included transfer requires its allowance period and scope")
        elif self.allowance_period is not None or self.allowance_scope is not None:
            raise ValueError("an allowance period or scope requires included transfer")
        return self


class SupplierCostTerms(ContractModel):
    """Per-node supplier terms, with bundled CPU, RAM and GPUs priced once as compute.

    Zero records a known free or included component. None records an unknown
    component. Transfer, setup charges and monthly caps do not enter hourly sums.
    """

    model_config = ConfigDict(frozen=True)

    currency: Literal["USD"] = "USD"
    source: str | None = Field(default=None, min_length=1)
    observed_at: datetime | None = None
    compute_hourly_micros: int | None = Field(default=None, ge=0)
    root_disk_hourly_micros: int | None = Field(default=None, ge=0)
    public_ipv4_hourly_micros: int | None = Field(default=None, ge=0)
    historical_unallocated_hourly_micros: int | None = Field(default=None, ge=0)
    """Recorded aggregate whose original component breakdown was not retained."""
    compute_monthly_cap_micros: int | None = Field(default=None, ge=0)
    setup_micros: int | None = Field(default=None, ge=0)
    billing_minimum_seconds: int | None = Field(default=None, ge=0)
    billing_quantum_seconds: int | None = Field(default=None, ge=1)
    network: SupplierNetworkTerms | None = None

    @model_validator(mode="after")
    def validate_historical_aggregate(self) -> SupplierCostTerms:
        if self.historical_unallocated_hourly_micros is not None and any(
            value is not None
            for value in (
                self.compute_hourly_micros,
                self.root_disk_hourly_micros,
                self.public_ipv4_hourly_micros,
            )
        ):
            raise ValueError("historical aggregate cannot be combined with hourly components")
        if self.compute_monthly_cap_micros is not None and self.compute_hourly_micros is None:
            raise ValueError("a compute monthly cap requires its hourly compute rate")
        return self

    @property
    def known_hourly_cost_micros(self) -> int | None:
        if self.historical_unallocated_hourly_micros is not None:
            return self.historical_unallocated_hourly_micros
        values = (
            self.compute_hourly_micros,
            self.root_disk_hourly_micros,
            self.public_ipv4_hourly_micros,
        )
        known = [value for value in values if value is not None]
        return sum(known) if known else None

    @property
    def complete_hourly_cost_micros(self) -> int | None:
        if (
            self.compute_hourly_micros is None
            or self.root_disk_hourly_micros is None
            or self.public_ipv4_hourly_micros is None
        ):
            return None
        return (
            self.compute_hourly_micros
            + self.root_disk_hourly_micros
            + self.public_ipv4_hourly_micros
        )


__all__ = [
    "SupplierCostTerms",
    "SupplierCpuUnit",
    "SupplierNetworkAllowancePeriod",
    "SupplierNetworkAllowanceScope",
    "SupplierNetworkTerms",
]
