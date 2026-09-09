from __future__ import annotations

from datetime import datetime

from pydantic import Field, model_validator

from shared.billing_plans import BillingPlanId, SubscriptionTermsVersion
from shared.contracts import ContractModel
from shared.enums import StringEnum
from shared.timestamps import utc_now

_UUID_PATTERN = r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"


class BillingAccountStatus(StringEnum):
    """Whether this account is in good standing.

    An account can owe money whatever it has agreed to pay for, so standing is
    its own fact rather than something read off what the account is subscribed
    to.
    """

    Active = "active"
    PastDue = "past_due"


class BillingAccount(ContractModel):
    """Who pays, and how they are named at the payment provider.

    Scoped to the user for the same reason a connected cloud account is: someone
    running dev, staging and prod holds three workspaces and one payment
    relationship, and a record per workspace would be three cards to keep in
    step. `shared.aws_connections` resolves the same way, through the owner.

    A row is written when an account signs in, before its session exists.
    It records the provider customer and any paid subscription. `plan` is
    `None` when the account has no recorded plan.
    """

    id: str = Field(pattern=_UUID_PATTERN)
    user_id: str = Field(pattern=_UUID_PATTERN)
    status: BillingAccountStatus = BillingAccountStatus.Active
    provider_customer_id: str = Field(default="", max_length=255)
    """The payment provider's identifier for this payer, empty until registered.

    Stored rather than looked up by email: the provider's own record can be
    renamed or duplicated, while the identifier names the object this platform
    created.
    """
    provider_subscription_id: str = Field(default="", max_length=255)
    """The paid plan subscription, empty when the account has none."""
    plan: BillingPlanId | None = None
    """Which plan this account is on, `None` when it is on none.

    Before provisioning, and again once the provider says the subscription has
    ended — the two are the same state and are told apart by nothing here,
    because what an account with no plan needs next is the same either way:
    provisioning, by the next billing surface it reaches.

    Written whenever the subscription's plan is established, changes or ends,
    including from the delivery that reports a change made at the provider — so
    this is a fast local read for the dashboard and for an upgrade, and never the
    only place a plan is recorded.
    """
    subscription_terms_version: SubscriptionTermsVersion | None = None
    scheduled_terms_version: SubscriptionTermsVersion | None = None
    scheduled_change_at: datetime | None = None
    payment_method_attached_at: datetime | None = None
    """When the current saved-card state began; `None` when no card is saved.

    Purchases and automatic reload require a saved card. Its presence does not
    grant credit or bypass admission; the balance and payment standing decide that.
    """
    complimentary_since: datetime | None = None
    """When an administrator waived usage charges; `None` without a waiver.

    Complimentary accounts receive Team entitlements. Usage retains its ledger
    price and a waived settlement. The held subscription remains unchanged.
    Platform authorization roles never grant or revoke this billing status.
    """
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def scheduled_terms_are_paired(self) -> BillingAccount:
        if (self.scheduled_terms_version is None) != (self.scheduled_change_at is None):
            raise ValueError("scheduled subscription terms require their effective time")
        return self


__all__ = [
    "BillingAccount",
    "BillingAccountStatus",
]
