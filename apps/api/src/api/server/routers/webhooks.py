from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response, status
from provider_stripe import SIGNATURE_HEADER, parse_event, verify_signature
from shared.errors import InvalidInputError
from shared.payments import PaymentEvent
from shared.timestamps import utc_now

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


async def _stripe_event(
    request: Request,
    services: ApiServices = Depends(current_services),
) -> PaymentEvent:
    settings = services.stripe_settings
    if not settings.webhooks_configured:
        LOGGER.error("billing: a stripe delivery arrived but no endpoint secret is configured")
        raise InvalidInputError("payment webhooks are not configured")

    declared = request.headers.get("content-length")
    if declared is not None and declared.isdigit() and int(declared) > MAX_DELIVERY_BYTES:
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
    return parse_event(body)


@router.post(
    "/stripe",
    status_code=status.HTTP_204_NO_CONTENT,
    include_in_schema=False,
    operation_id="receiveStripeWebhook",
)
def receive_stripe_webhook(
    event: Annotated[PaymentEvent, Depends(_stripe_event)],
    services: ApiServices = Depends(current_services),
) -> Response:
    """Take a delivery from the payment provider.

    Unauthenticated in the usual sense and signed instead: the caller is Stripe,
    which holds no token of ours, and the endpoint secret is what stands in for
    one. Kept out of the schema because the shape is Stripe's rather than this
    platform's, and publishing it would describe their API as if it were ours.

    Bodiless on success. The provider needs a 2xx and nothing else; anything it
    read from a body would be this platform inventing a protocol on top of one
    that already works. A 204 says the delivery landed without pretending there
    is a resource here to represent.
    """

    with services.context.database.session() as session:
        BillingWebhookService(session, services.payment_provider).apply(event=event)
        # The delivery claim and its effect commit together. A provider retry
        # must not find a claim for a change that never landed.
        session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
