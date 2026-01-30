"""Webhook handlers for external service integrations."""

from fastapi import APIRouter, HTTPException, Request
from loguru import logger
from polar_sdk.webhooks import WebhookVerificationError, validate_event
from starlette.responses import Response

from backend.config import app_config
from backend.database import get_db_context
from backend.database.users import SubscriptionState
from backend.services import get_subscription_service

webhooks_router = APIRouter(prefix="/webhooks", tags=["webhooks"])

# All subscription events that we handle
SUBSCRIPTION_EVENTS = {
    "subscription.created",
    "subscription.active",
    "subscription.updated",
    "subscription.canceled",
    "subscription.uncanceled",
    "subscription.revoked",
    "subscription.past_due",
}

# Events that indicate healthy subscription - reset to WITHIN_LIMITS
SUBSCRIPTION_HEALTHY_EVENTS = {
    "subscription.created",
    "subscription.active",
    "subscription.uncanceled",
}

# Events that indicate payment issues
SUBSCRIPTION_PAYMENT_FAILED_EVENTS = {
    "subscription.past_due",
}

# Events that indicate subscription ended
SUBSCRIPTION_REVOKED_EVENTS = {
    "subscription.revoked",
}


async def _update_user_subscription_state(
    external_id: str, new_state: SubscriptionState
) -> bool:
    """Update user's subscription_state by their external ID (workos_id).

    Returns True if user was found and updated, False otherwise.
    """
    async with get_db_context() as db:
        user = await db.users.get_by_workos_id(external_id)
        if not user:
            logger.warning(f"User not found for external_id: {external_id}")
            return False

        if user.subscription_state != new_state:
            user.subscription_state = new_state
            await db.users.update(user)
            logger.info(
                f"Updated subscription_state for {external_id} to {new_state.value}"
            )
        return True


@webhooks_router.post("/polar")
async def handle_polar_webhook(request: Request) -> Response:
    """Handle Polar webhook events.

    Validates the webhook signature and processes subscription events to:
    1. Invalidate the features cache for affected customers
    2. Update user subscription_state based on event type

    Returns 202 Accepted as per Polar webhook spec.
    """
    if not app_config.POLAR_WEBHOOK_SECRET:
        logger.warning("Polar webhook received but POLAR_WEBHOOK_SECRET not configured")
        raise HTTPException(status_code=503, detail="Webhook secret not configured")

    payload = await request.body()
    headers = dict(request.headers)

    try:
        event = validate_event(
            payload=payload,
            headers=headers,
            secret=app_config.POLAR_WEBHOOK_SECRET,
        )
    except WebhookVerificationError as e:
        logger.warning(f"Polar webhook signature verification failed: {e}")
        raise HTTPException(status_code=401, detail="Invalid webhook signature")

    event_type = event.type
    logger.info(f"Received Polar webhook: {event_type}")

    # Handle subscription events
    if event_type in SUBSCRIPTION_EVENTS:
        try:
            # Get customer external ID from the event
            customer = getattr(event.data, "customer", None)
            if not customer:
                logger.warning(f"Polar webhook {event_type} missing customer data")
                return Response(status_code=202)

            external_id = getattr(customer, "external_id", None)
            if not external_id:
                logger.warning(
                    f"Polar webhook {event_type} missing customer.external_id"
                )
                return Response(status_code=202)

            # Always clear the features cache on subscription events
            subscription_service = get_subscription_service()
            await subscription_service.clear_features_cache(external_id)
            logger.info(f"Cleared features cache for {external_id} due to {event_type}")

            # Update user subscription_state based on event type
            if event_type in SUBSCRIPTION_HEALTHY_EVENTS:
                await _update_user_subscription_state(
                    external_id, SubscriptionState.WITHIN_LIMITS
                )
            elif event_type in SUBSCRIPTION_PAYMENT_FAILED_EVENTS:
                await _update_user_subscription_state(
                    external_id, SubscriptionState.PAYMENT_FAILED
                )
            elif event_type in SUBSCRIPTION_REVOKED_EVENTS:
                await _update_user_subscription_state(
                    external_id, SubscriptionState.SUSPENDED
                )
            # subscription.updated and subscription.canceled don't change state
            # - updated: plan change, limits handled by features cache
            # - canceled: still active until period ends

        except Exception as e:
            logger.error(f"Error processing Polar webhook {event_type}: {e}")
            # Don't fail the webhook - Polar will retry

    # Return 202 Accepted as per Polar spec
    return Response(status_code=202)
