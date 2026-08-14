from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Request, Response, status
from provider_stripe import SIGNATURE_HEADER, parse_event, verify_signature
from shared.errors import InvalidInputError
from shared.payments import PaymentEvent
from shared.timestamps import utc_now
from starlette.concurrency import run_in_threadpool

from api.server.dependencies import current_services
from api.server.services import ApiServices
from billing import BillingWebhookService

LOGGER = logging.getLogger(__name__)

router = APIRouter(prefix="/webhooks", tags=["webhook"])

MAX_DELIVERY_BYTES = 1_048_576
"""The largest delivery this endpoint will read.

Stripe's own documented ceiling for an event payload, so nothing legitimate
approaches it.
"""


@router.post(
    "/stripe",
    status_code=status.HTTP_204_NO_CONTENT,
    include_in_schema=False,
    operation_id="receiveStripeWebhook",
)
async def receive_stripe_webhook(
    request: Request,
    services: ApiServices = Depends(current_services),
) -> Response:
    """Take a delivery from the payment provider.

    Unauthenticated in the usual sense and signed instead: the caller is Stripe,
    which holds no token of ours, and the endpoint secret is what stands in for
    one. Kept out of the schema because the shape is Stripe's rather than this
    platform's, and publishing it would describe their API as if it were ours.

    Bodiless on success. The provider needs a 2xx and nothing else — anything it
    reads from a body would be this platform inventing a protocol on top of one
    that already works — and a 204 says the delivery landed without pretending
    there is a resource here to represent.
    """

    settings = services.stripe_settings
    if not settings.webhooks_configured:
        # A public endpoint with no secret cannot tell Stripe from anyone else, so
        # it refuses everything rather than trusting anything. Loud, because the
        # symptom otherwise is a customer whose saved card is never charged.
        LOGGER.error("billing: a stripe delivery arrived but no endpoint secret is configured")
        raise InvalidInputError("payment webhooks are not configured")

    declared = request.headers.get("content-length")
    if declared is not None and declared.isdigit() and int(declared) > MAX_DELIVERY_BYTES:
        # Refused before it is read. The body has to be buffered whole to be
        # signed, and this endpoint takes anonymous POSTs, so an unbounded read
        # is memory anyone can spend.
        raise InvalidInputError("stripe delivery is larger than this endpoint accepts")
    body = await request.body()
    if len(body) > MAX_DELIVERY_BYTES:
        raise InvalidInputError("stripe delivery is larger than this endpoint accepts")
    signature = request.headers.get(SIGNATURE_HEADER)
    if not signature:
        raise InvalidInputError(f"{SIGNATURE_HEADER} is missing")
    verify_signature(
        payload=body,
        header=signature,
        secret=settings.webhook_secret.get_secret_value(),
        now=int(utc_now().timestamp()),
    )
    event = parse_event(body)

    # Off the event loop. Applying a delivery is a synchronous database
    # transaction wrapped around a call to the provider that can take its whole
    # timeout, and holding the loop for that would stop this process serving
    # anything else — dashboard, gateway, and agent registration included.
    await run_in_threadpool(_apply, services, event)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


def _apply(services: ApiServices, event: PaymentEvent) -> None:
    with services.context.database.session() as session:
        BillingWebhookService(session, services.payment_provider).apply(event=event)
        # One commit for the claim and its effect together. Committing the claim
        # on its own would make the provider's retry a no-op and lose a change
        # this platform never applied.
        session.commit()
