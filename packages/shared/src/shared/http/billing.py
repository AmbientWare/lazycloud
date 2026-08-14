from __future__ import annotations

from datetime import datetime

from pydantic import Field

from shared.billing_accounts import BillingAccountStatus
from shared.billing_plans import BillingPlanId
from shared.http.base import HttpModel


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


class BillingAllowanceResponse(HttpModel):
    """What this account may spend before this period costs it anything.

    The period is a pair of instants rather than a month name: it runs with the
    subscription's own cycle, which starts on the day the account was registered,
    and a calendar label would put a cost in the wrong one for every account that
    did not start on the first.

    `allowance_nanos` is the figure stamped on the period when it opened, not
    what the plan currently includes — changing what the platform grants must not
    restate the terms of a period a customer is already part-way through.
    """

    period_started_at: datetime
    period_ended_at: datetime
    allowance_nanos: int = Field(ge=0)
    spent_nanos: int = Field(ge=0)
    remaining_nanos: int
    """Signed, and negative once the allowance is overspent.

    Clamping it at zero would erase how far past the line an account is, which is
    the figure that says what this period's invoice will ask for beyond the plan.
    """


class BillingPlanResponse(HttpModel):
    """The plan an account holds, and the allowance its current cycle came with.

    One object rather than two fields, because the allowance is the plan's: the
    period is opened from the subscription's own cycle and stamped with what that
    plan includes, so an account on no plan has no period to report either.
    """

    id: BillingPlanId
    allowance: BillingAllowanceResponse | None = None
    """`None` between a cycle ending and the renewal that opens the next one.

    Distinct from a spent allowance and from no plan: the account is on a plan
    and no period covers this instant, which is a few minutes once a cycle. An
    empty period reported as a zero allowance would read as terms the customer is
    held to, when it is the absence of any.
    """


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


__all__ = [
    "BillingAllowanceResponse",
    "BillingHostedSessionRequest",
    "BillingHostedSessionResponse",
    "BillingPlanResponse",
    "BillingSummaryResponse",
]
