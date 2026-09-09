from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import Field

from shared.billing_accounts import BillingAccountStatus
from shared.billing_credits import CreditScope
from shared.billing_plans import BillingPlanId, SubscriptionTermsVersion
from shared.credit_payments import (
    MAX_CREDIT_PURCHASE_CENTS,
    MIN_CREDIT_PURCHASE_CENTS,
    CreditPaymentStatus,
)
from shared.http.base import HttpModel
from shared.http.pricing import PlanEntitlementsResponse
from shared.http.users import UserResponse


class BillingHostedSessionRequest(HttpModel):
    """Where to send the customer back to when they are done at the provider.

    Supplied by the caller rather than configured here because the dashboard
    knows which page the person was on, and returning everyone to one fixed
    location would lose that. Both are checked against the platform's own origin
    before they are used — an open redirect on a page that follows a card being
    saved is exactly the one worth abusing.
    """

    return_url: str = Field(min_length=1, max_length=2048)
    cancel_url: str = Field(default="", max_length=2048)
    """Where to return on abandoning the page. Falls back to `return_url`, which
    is the same page and correct: a customer who did not save a card should land
    where a customer who did lands, and find out there that nothing changed."""


class BillingHostedSessionResponse(HttpModel):
    """The page to send the customer to.

    A URL and nothing else. Card details are collected, stored, and shown by the
    payment provider, and never reach this platform — which is what keeps it
    outside the scope of cardholder data rules.
    """

    url: str = Field(min_length=1, max_length=2048)


class CreditPurchaseRequest(BillingHostedSessionRequest):
    request_key: UUID
    amount_cents: int = Field(
        ge=MIN_CREDIT_PURCHASE_CENTS, le=MAX_CREDIT_PURCHASE_CENTS, strict=True
    )


class CreditPurchaseResponse(HttpModel):
    id: UUID
    amount_nanos: int = Field(gt=0)
    status: CreditPaymentStatus
    checkout_url: str | None = None
    funded_at: datetime | None = None
    reversed_nanos: int = Field(ge=0)


class CreditBalanceResponse(HttpModel):
    purchased_nanos: int
    subscription_nanos: int
    trial_nanos: int
    held_nanos: int = Field(ge=0)
    debt_nanos: int = Field(ge=0)
    available_nanos: int = Field(ge=0)


class CreditSummaryResponse(HttpModel):
    ready: bool
    compute: CreditBalanceResponse
    storage_and_transfer: CreditBalanceResponse


class UsageBudgetResponse(HttpModel):
    month_started_at: datetime
    month_ended_at: datetime
    limit_nanos: int | None = Field(ge=0)
    spent_nanos: int = Field(ge=0)
    held_nanos: int = Field(ge=0)
    available_nanos: int | None = Field(ge=0)


class BillingPlanChangeRequest(HttpModel):
    """The exact published terms selected by the customer."""

    plan: BillingPlanId
    terms_version: SubscriptionTermsVersion


class BillingPlanResponse(HttpModel):
    """Purchased subscription terms and the current billing period."""

    id: BillingPlanId
    name: str
    """What to call this plan on screen, from the same card the pricing page uses.

    Sent rather than mapped in the browser, so the account summary needs no local
    plan-name map beside the catalog it fetches."""

    terms_version: SubscriptionTermsVersion | None
    monthly_nanos: int | None = Field(ge=0)
    included_nanos: int | None = Field(ge=0)
    credit_scope: CreditScope | None
    scheduled_terms_version: SubscriptionTermsVersion | None
    scheduled_change_at: datetime | None

    period_started_at: datetime | None
    period_ended_at: datetime | None


class BillingEntitlementUsageResponse(HttpModel):
    concurrent_cpu_containers: int = Field(ge=0)
    concurrent_gpus: int = Field(ge=0)
    workspaces: int = Field(ge=0)
    members: int = Field(ge=0)
    connected_clouds: int = Field(ge=0)
    custom_domains: int = Field(ge=0)


class BillingSummaryResponse(HttpModel):
    """What this account is on and where it stands.

    Scoped to the signed-in person, who is who the provider invoices — a
    workspace does not have its own payment relationship, its owner does.
    """

    status: BillingAccountStatus
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    plan: BillingPlanResponse | None = None
    """What this account is subscribed to, `None` when it is subscribed to nothing.

    Signing in puts an account on the free plan, so `None` is an account that
    never reached a billing surface — one an administrator minted — or one whose
    subscription the provider says has ended. Showing either an allowance would
    state terms nothing will hold them to, and both need the same thing next,
    which is to be put on a plan.
    """

    portal_available: bool
    """Whether there is anything at the provider for this account to manage.

    True from an account's first sign-in, which is when its customer record is
    created. False only for an account that has never signed in and so is named
    nowhere at the provider; the management page has nobody to show for one of
    those, so the dashboard offers card setup instead of a dead link.
    """

    payment_method_on_file: bool
    """Whether a card is saved, which is a different question from the one above.

    `portal_available` is true from the moment a customer record exists, so it
    cannot tell an account that has saved a card from one that has not. This can,
    and has to: how much an account is given, and whether its running work is
    stopped when that is spent, both turn on it — so a customer needs to see
    which state they are in and what changes if they attach one.
    """

    entitlements: PlanEntitlementsResponse | None = None
    """What the current plan grants, absent when the account has no plan."""

    usage: BillingEntitlementUsageResponse
    """Current account-wide consumption of every measured entitlement."""

    complimentary_since: datetime | None = None
    """When an administrator waived this account's bill, `None` while nobody has.

    Usage is still priced and shown, and nothing of it is owed. `plan` beside
    it is the subscription the account still holds and returns to when the
    waiver is withdrawn. `entitlements` are the waiver's, which are what the
    account is held to.
    """

    plan_change_pending: bool
    """Whether a change of plan is still waiting on an outcome.

    `plan` above says what the account holds, which is not what somebody who has
    just asked to move is looking for. A change nobody could settle is retried
    for hours, and a surface without this shows the old plan beside a live button
    that answers `409` — a pending change the customer cannot see is the one
    thing this field exists to prevent.
    """


class BillingAccountAdminResponse(HttpModel):
    """One account as an administrator sees it: the person, and what they owe.

    `plan` and `status` are absent together for an account billing has never
    written a row for, which is one that has not signed in yet. Such an account
    can still be waived ahead of time, which is what `complimentary_since` on a
    row with no plan means.
    """

    user: UserResponse
    status: BillingAccountStatus | None = None
    plan: BillingPlanId | None = None
    payment_method_on_file: bool = False
    complimentary_since: datetime | None = None
    recent_cost_nanos: int = Field(ge=0)
    """What this account's usage cost over the trailing window, waived or not."""
    recent_cost_since: datetime
    """Where that window starts; it ends at the moment of the request."""


class BillingAccountAdminListResponse(HttpModel):
    data: list[BillingAccountAdminResponse] = Field(default_factory=list)
    next: str = ""


class BillingComplimentaryRequest(HttpModel):
    complimentary: bool


__all__ = [
    "BillingAccountAdminListResponse",
    "BillingAccountAdminResponse",
    "BillingComplimentaryRequest",
    "BillingEntitlementUsageResponse",
    "BillingHostedSessionRequest",
    "BillingHostedSessionResponse",
    "BillingPlanChangeRequest",
    "BillingPlanResponse",
    "BillingSummaryResponse",
    "CreditBalanceResponse",
    "CreditPurchaseRequest",
    "CreditPurchaseResponse",
    "CreditSummaryResponse",
    "UsageBudgetResponse",
]
