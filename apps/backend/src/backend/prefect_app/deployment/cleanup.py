"""Cleanup flow for stale pending deployments."""

from datetime import UTC, datetime, timedelta

from loguru import logger
from models.deployments import DeploymentStates
from prefect import flow

from backend.database import get_db_context


@flow(log_prints=True)
async def cleanup_stale_pending(hours_old: int = 24) -> dict:
    """Clean up PENDING deployments older than TTL.

    Finds deployments that have been in PENDING state for longer than
    the threshold and soft-deletes them, releasing the quota slot.

    This handles orphaned deployments from:
    - Failed builds
    - CLI crashes or connection errors
    - User abandonment
    """
    threshold = datetime.now(UTC) - timedelta(hours=hours_old)

    logger.info(
        f"Starting stale pending cleanup (checking deployments > {hours_old} hours old)"
    )

    async with get_db_context() as db:
        stale_deployments = await db.compose_deployments.find_stale_pending(threshold)

    logger.info(f"Found {len(stale_deployments)} stale PENDING deployments")

    cleaned = 0
    errors = 0

    for deployment in stale_deployments:
        try:
            async with get_db_context() as db:
                deployment_locked = await db.compose_deployments.get_by_id(
                    deployment.id, with_lock=True
                )

                if deployment_locked is None:
                    logger.warning(
                        f"Deployment {deployment.id} not found (may have been deleted)"
                    )
                    continue

                # Verify still in PENDING state (may have changed since query)
                if deployment_locked.state != DeploymentStates.PENDING:
                    logger.debug(
                        f"Deployment {deployment.id} is now in state {deployment_locked.state}, skipping"
                    )
                    continue

                # Soft delete
                deployment_locked.state = DeploymentStates.DELETED
                deployment_locked.deleted_at = datetime.now(UTC)
                deployment_locked.status_message = (
                    f"Auto-cancelled: pending for more than {hours_old} hours"
                )
                await db.compose_deployments.update(deployment_locked)

            logger.info(
                f"Cleaned up stale deployment: {deployment.id} "
                f"(name={deployment.name}, created={deployment.created_at})"
            )
            cleaned += 1

        except Exception as e:
            error_type = type(e).__name__
            logger.error(
                f"Error cleaning up deployment {deployment.id}: {error_type} - {str(e)}",
                exc_info=True,
            )
            errors += 1

    result = {
        "checked": len(stale_deployments),
        "cleaned": cleaned,
        "errors": errors,
    }

    logger.info(
        f"Cleanup complete: checked {result['checked']}, "
        f"cleaned {result['cleaned']}, errors {result['errors']}"
    )

    return result


# Run daily at 3 AM
cleanup_stale_pending_deployment = cleanup_stale_pending.to_deployment(
    name="cleanup-stale-pending",
    cron="0 3 * * *",
)
