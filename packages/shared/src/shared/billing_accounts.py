from __future__ import annotations

from datetime import datetime

from pydantic import ConfigDict, Field

from shared.contracts import ContractModel
from shared.enums import StringEnum
from shared.timestamps import utc_now

_UUID_PATTERN = r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"


class BillingPlan(StringEnum):
    """What an account has agreed to pay for.

    `Free` is what an account is on before it has agreed to anything, which is
    why it needs no stored row: an account nobody has billed and an account that
    chose the free plan behave identically, and separate states would mean
    answering "which" on every read for no difference in outcome.
    """

    Free = "free"
    Team = "team"


class BillingAccountStatus(StringEnum):
    """Whether this account is in good standing.

    Distinct from the plan because the two change for unrelated reasons: a plan
    changes when someone chooses, standing changes when a payment succeeds or
    fails. An account can owe money on any plan.
    """

    Active = "active"
    PastDue = "past_due"


class BillingAccount(ContractModel):
    """Who pays, on what terms — one per account, never per workspace.

    Scoped to the user for the same reason a connected cloud account is: someone
    running dev, staging and prod holds three workspaces and one payment
    relationship, and a record per workspace would be three cards to keep in
    step. `shared.aws_connections` resolves the same way, through the owner.

    A row exists only once an account has agreed to pay. A workspace that has
    only ever run free work has none and needs none.
    """

    id: str = Field(pattern=_UUID_PATTERN)
    user_id: str = Field(pattern=_UUID_PATTERN)
    plan: BillingPlan = BillingPlan.Free
    status: BillingAccountStatus = BillingAccountStatus.Active
    provider_customer_id: str = Field(default="", max_length=255)
    """The payment provider's identifier for this payer, empty until registered.

    Stored rather than looked up by email: the provider's own record can be
    renamed or duplicated, while the identifier names the object this platform
    created.
    """
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)

    @property
    def state(self) -> BillingAccountState:
        return BillingAccountState(
            plan=self.plan,
            status=self.status,
            provider_customer_id=self.provider_customer_id,
        )


class BillingAccountState(ContractModel):
    """What is in force for a workspace, whether or not a row was ever written.

    Separate from `BillingAccount` so the free default carries no identifier: a
    synthesised row would be a record that looks persisted, and the first caller
    to write back through its id would write to nothing.

    Frozen because the free default is one shared instance. Assignment through it
    would otherwise reach every workspace that has never paid, so a single caller
    marking its own copy past-due would put every free account in arrears.
    """

    model_config = ConfigDict(frozen=True)

    plan: BillingPlan = BillingPlan.Free
    status: BillingAccountStatus = BillingAccountStatus.Active
    provider_customer_id: str = Field(default="", max_length=255)


FREE_BILLING_ACCOUNT = BillingAccountState()
"""What a workspace resolves to before anyone has agreed to pay for anything."""


__all__ = [
    "FREE_BILLING_ACCOUNT",
    "BillingAccount",
    "BillingAccountState",
    "BillingAccountStatus",
    "BillingPlan",
]
