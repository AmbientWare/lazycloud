"""Subscription monitoring cron job."""

from typing import Any

from loguru import logger

from backend.database import get_db_context
from backend.database.users import SubscriptionState, UserStatus
from backend.services import get_subscription_service
from backend.services.exceptions import NoActiveSubscriptionError


async def monitor_subscription_states_job(ctx: dict[str, Any]) -> dict[str, Any]:
    """Monitor users' subscription states, check actual usage vs limits, and update status.

    Args:
        ctx: SAQ job context

    Returns:
        Result dict with checked count, overage count, and updated count
    """
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
    error_count = 0

    for user in users:
        try:
            features = await subscription_service.get_user_features(user.workos_id)
            state_before = user.subscription_state

            user_after = (
                await subscription_service._audit_and_update_subscription_state(
                    user.id, features
                )
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
                    f"User {user.email} (ID: {user.id}, WorkOS ID: {user.workos_id}) "
                    f"has OVER_LIMITS subscription state"
                )

        except NoActiveSubscriptionError:
            # Users without subscriptions (e.g., admin/test users) are skipped gracefully
            logger.warning(
                f"Skipping user {user.email} (ID: {user.id}) - no active subscription"
            )
        except Exception:
            error_count += 1
            logger.exception(
                f"Error checking subscription state for user {user.email} "
                f"(ID: {user.id}, WorkOS ID: {user.workos_id})"
            )

    logger.info(
        f"Subscription monitoring complete. Checked {len(users)} users, "
        f"found {overage_count} with overage, updated {updated_count} status(es), "
        f"{error_count} error(s)"
    )

    return {
        "checked": len(users),
        "overage_count": overage_count,
        "updated": updated_count,
        "errors": error_count,
    }
