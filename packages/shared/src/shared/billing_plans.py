from __future__ import annotations

from collections.abc import Mapping

from pydantic import ConfigDict, Field, field_validator

from shared.billing_accounts import BillingPlan
from shared.contracts import ContractModel


class BillingPlanConfig(ContractModel):
    """What one plan costs and what it comes with.

    Included usage is a property of every plan rather than a rule about one of
    them. A free tier that comes with a dollar of compute and a team tier that
    comes with a hundred differ by their numbers, not by their machinery, and a
    plan whose allowance is written into a branch somewhere cannot be changed
    without changing code.

    Everything here is a rate or an allowance, never a decision about a
    particular account: what an account is on lives on the account, and what that
    plan means lives here.
    """

    model_config = ConfigDict(frozen=True)

    plan: BillingPlan
    monthly_price_nanos: int = Field(default=0, ge=0)
    """Charged whether or not anything runs. Zero is a real answer, not an
    absent one — a usage-only plan is a subscription of zero."""

    included_cost_nanos: int = Field(default=0, ge=0)
    """Usage this plan absorbs before anything is billed for it.

    Expressed in money rather than in seconds so one allowance covers every
    metric at once. An allowance per metric would have to be re-derived every
    time a rate moved, and would let an account exhaust its GPU credit while
    sitting on unspendable CPU credit.
    """

    currency: str = Field(min_length=3, max_length=3)

    @field_validator("currency")
    @classmethod
    def currency_code(cls, value: str) -> str:
        normalized = value.strip().upper()
        if len(normalized) != 3 or not normalized.isalpha():
            raise ValueError("billing plan currency must be a three-letter code")
        return normalized

    def charge_for(self, usage_cost_nanos: int) -> int:
        """What a period on this plan bills, given what its usage came to.

        The allowance is spent against usage and never refunded as credit: an
        account that used less than it came with pays the subscription and
        nothing more, and does not carry the remainder forward. A period is the
        whole of what it settles.
        """

        billable_usage = max(usage_cost_nanos - self.included_cost_nanos, 0)
        return self.monthly_price_nanos + billable_usage


class BillingPlanCatalog(ContractModel):
    """Every plan an account can be on, and what each one means."""

    model_config = ConfigDict(frozen=True)

    plans: tuple[BillingPlanConfig, ...]

    @field_validator("plans")
    @classmethod
    def plans_cover_every_plan_exactly_once(
        cls, value: tuple[BillingPlanConfig, ...]
    ) -> tuple[BillingPlanConfig, ...]:
        seen = [config.plan for config in value]
        if len(set(seen)) != len(seen):
            raise ValueError("billing plan catalog lists a plan twice")
        if set(seen) != set(BillingPlan):
            missing = ", ".join(sorted(plan.value for plan in BillingPlan if plan not in seen))
            raise ValueError(f"billing plan catalog is missing: {missing}")
        return value

    def for_plan(self, plan: BillingPlan) -> BillingPlanConfig:
        """What this plan costs and includes.

        Total by construction: the catalog is refused unless it covers every
        plan, so an account can never be on one nothing prices.
        """

        return self._by_plan[plan]

    @property
    def _by_plan(self) -> Mapping[BillingPlan, BillingPlanConfig]:
        return {config.plan: config for config in self.plans}


DEFAULT_BILLING_PLANS = BillingPlanCatalog(
    plans=(
        BillingPlanConfig(
            plan=BillingPlan.Free,
            monthly_price_nanos=0,
            included_cost_nanos=1_000_000_000,
            currency="USD",
        ),
        BillingPlanConfig(
            plan=BillingPlan.Team,
            monthly_price_nanos=200_000_000_000,
            included_cost_nanos=100_000_000_000,
            currency="USD",
        ),
    ),
)
"""The plans as they stand: a dollar of compute free, and a team plan at $200 a
month that comes with $100 of it.

Nanodollars throughout, so $1 is 1e9. These are the numbers to change when a
price changes; nothing reads a plan's allowance from anywhere else.
"""


__all__ = [
    "DEFAULT_BILLING_PLANS",
    "BillingPlanCatalog",
    "BillingPlanConfig",
]
