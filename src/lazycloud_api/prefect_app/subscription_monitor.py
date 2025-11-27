from loguru import logger
from prefect import flow

from lazycloud_api.database import get_db_context
from lazycloud_api.database.users import SubscriptionState, UserStatus
from lazycloud_api.services import get_subscription_service

# TODO: This is only temporary until we have a webhooks handler for subscription updates.


@flow(log_prints=True)
async def monitor_subscription_states():
    """Monitor users' subscription states, check actual usage vs limits, and update status."""
    logger.info("Starting subscription state monitoring")

    # Get all active users
    async with get_db_context() as db:
        users = await db.users.find(
            filters={"status": UserStatus.ACTIVE},
        )

    if not users:
        logger.info("No active users found")
        return {"checked": 0, "overage_count": 0, "updated": 0}

    logger.info(f"Checking subscription states for {len(users)} active users")

    subscription_service = get_subscription_service()
    overage_count = 0
    updated_count = 0

    for user in users:
        features = await subscription_service.get_user_features(user.clerk_id)
        state_before = user.subscription_state

        user_after = await subscription_service._audit_and_update_subscription_state(
            user.id, features
        )

        if not user_after:
            continue

        if user_after.subscription_state != state_before:
            updated_count += 1
            logger.info(
                f"Updated user {user.email} (ID: {user.id}) subscription_state from "
                f"{state_before.value} to {user_after.subscription_state.value}"
            )

        if user_after.subscription_state == SubscriptionState.OVER_LIMITS:
            overage_count += 1
            logger.warning(
                f"User {user.email} (ID: {user.id}, Clerk ID: {user.clerk_id}) "
                f"has OVER_LIMITS subscription state"
            )

    logger.info(
        f"Subscription monitoring complete. Checked {len(users)} users, "
        f"found {overage_count} with overage, updated {updated_count} status(es)"
    )

    return {
        "checked": len(users),
        "overage_count": overage_count,
        "updated": updated_count,
    }


# Create deployment with cron schedule (runs every 4 hours)
monitor_subscription_states_deployment = monitor_subscription_states.to_deployment(
    name="monitor-subscription-states",
    cron="0 */4 * * *",  # Every 4 hours at the top of the hour
)
