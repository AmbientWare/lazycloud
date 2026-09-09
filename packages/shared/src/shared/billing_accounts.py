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

    A row is written when an account signs in, before its session exists, and
    carries the customer, the subscription and the grant that registration
    created. One that never signed in — an account that exists to own tokens —
    holds none of them, and `plan` is how a reader tells that apart from an
    account on the cheapest plan there is.
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
    """The subscription carrying the plan price and the metered prices, empty
    until the account is provisioned."""
    provider_credit_grant_id: str = Field(default="", max_length=255)
    """The grant carrying the included allowance for the current period, empty
    until one is issued."""
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
    """When this account's card was put on file, `None` while it holds none.

    What an account may spend before anyone can be charged is decided from this,
    so it has to be answerable without asking the provider: admission runs on the
    path that starts every container, and a network call there would let the
    provider's availability decide whether work runs.

    Set when a card is saved and cleared when the last one is removed, so it
    answers "is there a card now" rather than "was there ever one" — an account
    that attaches a card, spends against the larger allowance it buys and then
    detaches would otherwise keep that allowance for good.

    An instant rather than a flag because the sizing decision is made at a cycle
    boundary and a reader needs to know whether the card predates the cycle it is
    asking about. What it must not be read as is permission to spend *now*:
    allowance is a term of a period, and a card removed mid-cycle does not shrink
    the period already funded.

    `None` is not a judgement about whether the account can pay. An account that
    holds a card and cannot pay is `PastDue`, a different fact recorded beside
    this one.
    """
    complimentary_since: datetime | None = None
    """When an administrator waived this account's bill, `None` while nobody has.

    A standing rather than a plan. What the account gets is the Team plan's terms
    as if a card were on file, and what it owes is nothing. Its usage is priced
    into the ledger like anyone else's, and the meter event that would carry it
    to the provider is written as waived instead of sent. The subscription the
    account holds is left alone, so withdrawing this puts it back on that
    subscription's own terms with nothing to provision.

    Held here and not on the user, and never read off the platform role. Whether
    somebody pays is a billing fact; whether they administer the platform is an
    authorization fact; and an administrator demoted to member must not have
    their bill switched on by the same write. An instant rather than a flag so a
    reviewer can place it beside the usage it waived.
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
