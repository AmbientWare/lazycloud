from __future__ import annotations

from urllib.parse import urlparse

from billing.costs import BillingStanding, BillingStandingService
from fastapi import APIRouter, Depends, status
from shared.errors import InvalidInputError
from shared.http.billing import (
    BillingAllowanceResponse,
    BillingHostedSessionRequest,
    BillingHostedSessionResponse,
    BillingPlanResponse,
    BillingSummaryResponse,
)
from shared.payments import BILLING_CURRENCY
from shared.timestamps import utc_now

from api.server.auth import read_user, write_user
from api.server.dependencies import current_services
from api.server.services import ApiServices
from billing import BillingAccountService, BillingPlanChangeService, owned_workspace_id

router = APIRouter(prefix="/api/v1/billing", tags=["billing"])


@router.get(
    "/summary",
    response_model=BillingSummaryResponse,
    operation_id="get_billing_summary",
)
def billing_summary(
    user_id: read_user,
    services: ApiServices = Depends(current_services),
) -> BillingSummaryResponse:
    """What this account is on, and what it has left to spend.

    Answered about the signed-in person rather than a workspace: the provider
    invoices an account, and someone running dev, staging and prod holds three
    workspaces against one payment relationship.
    """

    with services.context.database.session() as session:
        standing = BillingStandingService(session).standing(user_id=user_id, at=utc_now())
    return _summary(standing)


@router.post(
    "/subscription",
    response_model=BillingSummaryResponse,
    operation_id="subscribe_billing_plan",
)
def subscribe_billing_plan(
    user_id: write_user,
    services: ApiServices = Depends(current_services),
) -> BillingSummaryResponse:
    """Move this account onto the Team plan, with the usage that plan includes.

    A price changed on the subscription the account already holds rather than a
    subscription created: the billing anniversary and the usage already metered
    this cycle both survive it, and the provider charges the prorated difference
    at once. `200` rather than `201` for that reason — every account already
    holds the subscription this modifies, and nothing here has a new address for
    a caller to follow.

    Bodiless, because there is one plan to move to: a body naming which would be
    a caller holding a catalog this platform publishes, and two callers naming it
    differently would be two customers on different subscriptions for one plan.

    Answers with the standing the account now has rather than the provider's
    record of the subscription. What the caller does next is decided by what they
    may spend and until when, and the subscription's own identifiers are the
    provider's business — publishing them here would put a second name for the
    same relationship on the wire.

    `409` where a change for this account is already being settled: the provider
    charges the proration inside the call, so letting a second one through would
    be a customer charged twice for one upgrade.
    """

    BillingPlanChangeService(
        database=services.context.database,
        payments=services.payment_provider,
        events=services.events,
    ).subscribe(user_id=user_id)
    with services.context.database.session() as session:
        return _summary(BillingStandingService(session).standing(user_id=user_id, at=utc_now()))


@router.post(
    "/card-session",
    response_model=BillingHostedSessionResponse,
    status_code=status.HTTP_201_CREATED,
    operation_id="start_billing_card_setup",
)
def start_billing_card_setup(
    request: BillingHostedSessionRequest,
    user_id: write_user,
    services: ApiServices = Depends(current_services),
) -> BillingHostedSessionResponse:
    """Begin saving a card, at the payment provider.

    The card is collected on the provider's own page: this returns somewhere to
    send the person, and the only thing that comes back to this platform is that
    a payment method now exists. Nothing here ever sees a card number, which is
    what keeps this repository outside the scope of cardholder data rules.

    The account is provisioned here too, because the page cannot be opened
    without a customer. Anyone who signed in was provisioned then, so this reads
    what already exists; an account reaching this page is one this platform will
    bill, so an administrator-minted one gets its customer, its subscription and
    its allowance here for the same reason sign-in gives them.
    """

    # Checked before anything else happens. A rejected address must not leave a
    # customer registered at the provider on the strength of a request that was
    # never going to be served.
    return_url = _own_url(services, request.return_url)
    cancel_url = _own_url(services, request.cancel_url or request.return_url)
    provider = services.payment_provider()
    with services.context.database.session() as session:
        account = BillingAccountService(session).billing_account_for(
            provider,
            user_id=user_id,
            workspace_id=owned_workspace_id(session, user_id),
        )
        session.commit()
    session_url = provider.card_setup_session(
        provider_customer_id=account.provider_customer_id,
        # Required by the provider even though the page charges nothing, and it
        # has to be the currency the customer is later charged in.
        currency=BILLING_CURRENCY,
        success_url=return_url,
        cancel_url=cancel_url,
    )
    return BillingHostedSessionResponse(url=session_url.url)


@router.post(
    "/portal-session",
    response_model=BillingHostedSessionResponse,
    status_code=status.HTTP_201_CREATED,
    operation_id="start_billing_portal",
)
def start_billing_portal(
    request: BillingHostedSessionRequest,
    user_id: write_user,
    services: ApiServices = Depends(current_services),
) -> BillingHostedSessionResponse:
    """Open the provider's page for managing what this platform bills.

    Replacing a card and reading past invoices. Refused for an account named
    nowhere at the provider — one that has never signed in — because there is
    nothing there to manage and the provider has nobody to show; such a caller
    wants the card page instead.
    """

    return_url = _own_url(services, request.return_url)
    with services.context.database.session() as session:
        account = BillingAccountService(session).payment_account_for(user_id=user_id)
    session_url = services.payment_provider().customer_portal_session(
        provider_customer_id=account.provider_customer_id,
        return_url=return_url,
    )
    return BillingHostedSessionResponse(url=session_url.url)


def _summary(standing: BillingStanding) -> BillingSummaryResponse:
    allowance = standing.allowance
    return BillingSummaryResponse(
        status=standing.status,
        currency=BILLING_CURRENCY,
        plan=(
            BillingPlanResponse(
                id=standing.plan,
                allowance=(
                    BillingAllowanceResponse(
                        period_started_at=allowance.started_at,
                        period_ended_at=allowance.ended_at,
                        allowance_nanos=allowance.allowance_nanos,
                        spent_nanos=allowance.spent_nanos,
                        remaining_nanos=allowance.remaining_nanos,
                    )
                    if allowance is not None
                    else None
                ),
            )
            if standing.plan is not None
            else None
        ),
        portal_available=standing.portal_available,
    )


def _own_url(services: ApiServices, candidate: str) -> str:
    """Refuse to send anyone anywhere but back here.

    The provider redirects the person to whatever this hands it, so an
    unchecked value is an open redirect on the page that follows saving a card —
    which is the one worth abusing, because the person arriving at it is
    expecting to be somewhere unfamiliar and to have just done something with
    their money.
    """

    try:
        parsed = urlparse(candidate)
    except ValueError as exc:
        # `urlparse` refuses a host that NFKC-normalizes into a delimiter, which
        # is a hostile address rather than an unexpected one — it must end as the
        # refusal this route promises, not as an unhandled error.
        raise InvalidInputError("a billing return address must be a valid URL") from exc
    expected = urlparse(services.gateway_settings.public_http_url)
    if parsed.scheme != expected.scheme or parsed.netloc != expected.netloc:
        raise InvalidInputError("a billing return address must be on this platform")
    return candidate
