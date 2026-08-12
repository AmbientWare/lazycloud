from __future__ import annotations

from urllib.parse import urlparse

from database.repositories.identity import WorkspaceMemberRepository
from fastapi import APIRouter, Depends, status
from shared.billing_plans import DEFAULT_BILLING_PLANS
from shared.errors import InvalidInputError, NotFoundError
from shared.http.billing import BillingHostedSessionRequest, BillingHostedSessionResponse
from sqlalchemy.orm import Session

from api.server.auth import write_user
from api.server.dependencies import current_services
from api.server.services import ApiServices
from billing import BillingAccountService

router = APIRouter(prefix="/api/v1/billing", tags=["billing"])


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

    Registering the customer happens here too, because the page cannot be opened
    without one — which is the moment a person first sets out to pay, and the
    right moment for the account row to start existing.
    """

    # Checked before anything else happens. A rejected address must not leave a
    # customer registered at the provider on the strength of a request that was
    # never going to be served.
    return_url = _own_url(services, request.return_url)
    cancel_url = _own_url(services, request.cancel_url or request.return_url)
    provider = services.payment_provider()
    with services.context.database.session() as session:
        account = BillingAccountService(session).payment_customer_for(
            provider,
            user_id=user_id,
            workspace_id=_workspace_of(session, user_id),
        )
        session.commit()
    session_url = provider.card_setup_session(
        provider_customer_id=account.provider_customer_id,
        currency=DEFAULT_BILLING_PLANS.for_plan(account.plan).currency,
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

    Replacing a card and reading past invoices. Refused for an account that has
    never registered to pay, because there is nothing there to manage and the
    provider has nobody to show — such a caller wants the card page instead.
    """

    return_url = _own_url(services, request.return_url)
    with services.context.database.session() as session:
        account = BillingAccountService(session).payment_account_for(user_id=user_id)
    session_url = services.payment_provider().customer_portal_session(
        provider_customer_id=account.provider_customer_id,
        return_url=return_url,
    )
    return BillingHostedSessionResponse(url=session_url.url)


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


def _workspace_of(session: Session, user_id: str) -> str:
    """A workspace to stamp on the provider's record of this customer.

    Traceability only — the account is the person, not the workspace — but a
    payment arriving out of band is far easier to place with one attached.
    """

    owned = WorkspaceMemberRepository(session).owned_workspace_ids(user_id)
    if not owned:
        raise NotFoundError(f"no workspace to bill for user: {user_id}")
    return owned[0]
