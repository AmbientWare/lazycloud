from loguru import logger
from prefect import flow

from lazycloud_api.database import get_db_context
from lazycloud_api.prefect_app.deployment.destroy import destroy_compose_task


@flow(log_prints=True)
async def cleanup_orphaned_deployments() -> dict:
    """Clean up deployments in deleted workspaces that haven't been cleaned up yet."""
    logger.info("Starting cleanup of orphaned deployments in deleted workspaces")

    # Find orphaned deployments using repository method
    async with get_db_context() as db:
        orphaned_deployments = (
            await db.compose_deployments.find_orphaned_in_deleted_workspaces()
        )

    if not orphaned_deployments:
        logger.info("No orphaned deployments found")
        return {
            "found": 0,
            "triggered": 0,
            "skipped": 0,
        }

    logger.info(f"Found {len(orphaned_deployments)} orphaned deployments")

    triggered = 0
    skipped = 0

    for deployment in orphaned_deployments:
        if not deployment.id:
            skipped += 1
            continue

        try:
            # Use transaction with lock to prevent race conditions
            # Re-check that current_task_run_id is still NULL before triggering
            async with get_db_context() as db:
                deployment_check = await db.compose_deployments.get_by_id(
                    deployment.id, with_lock=True, include_deleted=True
                )

                # Double-check: if task_run_id was set by another process, skip
                if not deployment_check or deployment_check.current_task_run_id:
                    logger.debug(
                        f"Skipping deployment {deployment.id}: "
                        f"task_run_id already set or deployment not found"
                    )
                    skipped += 1
                    continue

                # Trigger task and set task_run_id atomically
                logger.info(
                    f"Triggering cleanup for orphaned deployment {deployment.id} "
                    f"(workspace {deployment.workspace_id}, state: {deployment.state})"
                )
                task_future = destroy_compose_task.delay(
                    deployment_id=str(deployment.id)
                )

                # Set task_run_id to prevent duplicate triggers
                deployment_check.current_task_run_id = task_future.task_run_id
                async with get_db_context() as db:
                    await db.compose_deployments.update(deployment_check)

                triggered += 1

        except Exception as e:
            logger.warning(
                f"Failed to trigger cleanup task for deployment {deployment.id}: {e}"
            )
            skipped += 1

    logger.info(
        f"Cleanup complete: {triggered} tasks triggered, {skipped} skipped, "
        f"{len(orphaned_deployments)} total found"
    )

    return {
        "found": len(orphaned_deployments),
        "triggered": triggered,
        "skipped": skipped,
    }


cleanup_orphaned_deployments_deployment = cleanup_orphaned_deployments.to_deployment(
    name="cleanup-orphaned-deployments",
    cron="* * * * *",  # Every minute
)
