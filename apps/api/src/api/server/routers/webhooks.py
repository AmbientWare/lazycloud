from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response, status
from provider_resend import ID_HEADER as RESEND_ID_HEADER
from provider_resend import SIGNATURE_HEADER as RESEND_SIGNATURE_HEADER
from provider_resend import TIMESTAMP_HEADER as RESEND_TIMESTAMP_HEADER
from provider_resend import EmailDeliveryEvent as ResendDeliveryEvent
from provider_resend import parse_event as resend_parse_event
from provider_resend import verify_signature as resend_verify_signature
from provider_stripe import SIGNATURE_HEADER, parse_event, verify_signature
from shared.errors import InvalidInputError
from shared.payments import PaymentEvent
from shared.timestamps import utc_now

from api.server.dependencies import current_services
from api.server.services import ApiServices
from billing import BillingWebhookService
from notifications import DeliveryReport, record_delivery

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


async def _resend_event(
    request: Request,
    services: ApiServices = Depends(current_services),
) -> ResendDeliveryEvent | None:
    settings = services.resend_settings
    if not settings.webhooks_configured:
        LOGGER.error("email: a resend delivery arrived but no endpoint secret is configured")
        raise InvalidInputError("email webhooks are not configured")

    declared = request.headers.get("content-length")
    if declared is not None and declared.isdigit() and int(declared) > MAX_DELIVERY_BYTES:
        raise InvalidInputError("resend delivery is larger than this endpoint accepts")
    body = await request.body()
    if len(body) > MAX_DELIVERY_BYTES:
        raise InvalidInputError("resend delivery is larger than this endpoint accepts")

    message_id = request.headers.get(RESEND_ID_HEADER)
    timestamp = request.headers.get(RESEND_TIMESTAMP_HEADER)
    signature = request.headers.get(RESEND_SIGNATURE_HEADER)
    if not (message_id and timestamp and signature):
        raise InvalidInputError("resend delivery is missing its signature headers")
    resend_verify_signature(
        payload=body,
        message_id=message_id,
        timestamp=timestamp,
        signature_header=signature,
        secret=settings.webhook_secret.get_secret_value(),
        now=int(utc_now().timestamp()),
    )
    return resend_parse_event(body)


@router.post(
    "/resend",
    status_code=status.HTTP_204_NO_CONTENT,
    include_in_schema=False,
    operation_id="receiveResendWebhook",
)
def receive_resend_webhook(
    event: Annotated[ResendDeliveryEvent | None, Depends(_resend_event)],
    services: ApiServices = Depends(current_services),
) -> Response:
    """Take a delivery report from the email provider.

    Signed rather than authenticated, like the payment endpoint above: the caller
    is Resend, which holds no token of ours. This is the only way the platform
    learns that a message it sent successfully reached nobody, because accepting
    a message and delivering it are different events minutes apart.

    An event kind this platform does not read is answered 204 rather than
    refused. A failure would have Resend retry, and eventually disable the
    endpoint, over messages that were never a problem.
    """

    if event is not None:
        with services.context.database.session() as session:
            record_delivery(
                session,
                DeliveryReport(
                    provider_message_id=event.provider_message_id,
                    state=event.state,
                    occurred_at=event.occurred_at,
                    detail=event.detail,
                ),
            )
            session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
