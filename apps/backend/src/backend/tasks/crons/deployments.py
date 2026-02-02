"""Deployment-related cron jobs: reconciliation, cleanup, and orphan handling."""

from datetime import UTC, datetime, timedelta
from typing import Any

from loguru import logger
from models.deployments import DeploymentStates
from saq.types import Context

from backend.config import app_config
from backend.database import get_db_context
from backend.services.k8s import create_release_name
from backend.services.k8s.helm_manager import HelmManager
from backend.tasks.client import run_destroy_compose


async def reconcile_rollback_states_job(
    ctx: Context,
    minutes_old: int | None = None,
) -> dict[str, Any]:
    """Reconcile Helm and database states for stuck deployments.

    Args:
        ctx: SAQ job context
        minutes_old: How old (in minutes) a stuck deployment must be to reconcile

    Returns:
        Result dict with reconciliation stats
    """
    if minutes_old is None:
        minutes_old = app_config.ROLLBACK_RECONCILIATION_INTERVAL_MINUTES

    logger.info(
        f"Starting rollback state reconciliation (checking deployments > {minutes_old} minutes old)"
    )

    async with get_db_context() as db:
        stuck_deployments = await db.compose_deployments.find_stuck_deploying(
            minutes_old
        )

    logger.info(
        f"Found {len(stuck_deployments)} deployments in DEPLOYING state older than {minutes_old} minutes"
    )

    reconciled = 0
    errors = 0
    skipped = 0

    for deployment in stuck_deployments:
        try:
            name = create_release_name(deployment.workspace_id, deployment.name)
            namespace = deployment.namespace

            if not name:
                logger.warning(f"Skipping deployment {deployment.id}: no release name")
                skipped += 1
                continue

            # Create HelmManager with the deployment's cluster_id
            helm_manager = HelmManager(deployment.cluster_id)
            helm_revision = await helm_manager._get_latest_revision(name, namespace)
            db_revision = deployment.current_helm_revision

            if helm_revision is None:
                logger.warning(
                    f"Deployment {deployment.id} ({name}): Helm release not found. "
                    "May have been deleted externally."
                )
                skipped += 1
                continue

            history = await helm_manager.get_history(name, namespace)
            history_revisions = [
                item.get("revision") for item in history if item.get("revision")
            ]

            if helm_revision == db_revision and db_revision in history_revisions:
                logger.debug(
                    f"Deployment {deployment.id} ({name}): Helm ({helm_revision}) and DB ({db_revision}) match. No action needed."
                )
                skipped += 1
                continue

            if (
                db_revision not in history_revisions
                and helm_revision in history_revisions
            ):
                logger.info(
                    f"Deployment {deployment.id} ({name}): DB revision {db_revision} not in history "
                    f"(only last {len(history_revisions)} revisions kept). Syncing to Helm revision {helm_revision}."
                )
            elif helm_revision != db_revision:
                logger.info(
                    f"Deployment {deployment.id} ({name}): Mismatch detected - Helm: {helm_revision}, DB: {db_revision}. Syncing DB to match Helm."
                )
            else:
                skipped += 1
                continue

            async with get_db_context() as db:
                deployment_locked = await db.compose_deployments.get_by_id(
                    deployment.id, with_lock=True
                )
                if deployment_locked:
                    deployment_locked.current_helm_revision = helm_revision
                    deployment_locked.state = DeploymentStates.DEPLOYED
                    deployment_locked.status_message = (
                        f"Reconciled: DB synced to Helm revision {helm_revision} "
                        f"(was stuck in DEPLOYING state)"
                    )
                    deployment_locked.current_task_run_id = None
                    await db.compose_deployments.update(deployment_locked)

            logger.info(
                f"Successfully reconciled deployment {deployment.id}: synced DB revision to {helm_revision}"
            )
            reconciled += 1

        except Exception as e:
            error_type = type(e).__name__
            logger.error(
                f"Error reconciling deployment {deployment.id}: {error_type} - {str(e)}",
                exc_info=True,
            )
            errors += 1

    result = {
        "checked": len(stuck_deployments),
        "reconciled": reconciled,
        "skipped": skipped,
        "errors": errors,
    }

    logger.info(
        f"Reconciliation complete: checked {result['checked']}, "
        f"reconciled {result['reconciled']}, skipped {result['skipped']}, errors {result['errors']}"
    )

    return result


async def cleanup_stale_pending_job(
    ctx: Context,
    hours_old: int = 24,
) -> dict[str, Any]:
    """Clean up PENDING deployments older than TTL.

    Finds deployments that have been in PENDING state for longer than
    the threshold and soft-deletes them, releasing the quota slot.

    This handles orphaned deployments from:
    - Failed builds
    - CLI crashes or connection errors
    - User abandonment

    Args:
        ctx: SAQ job context
        hours_old: How old (in hours) a pending deployment must be to clean up

    Returns:
        Result dict with cleanup stats
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


async def cleanup_orphaned_deployments_job(ctx: Context) -> dict[str, Any]:
    """Clean up deployments in deleted workspaces that haven't been cleaned up yet.

    Args:
        ctx: SAQ job context

    Returns:
        Result dict with cleanup stats
    """
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

                # Trigger destroy job and set task_run_id atomically
                logger.info(
                    f"Triggering cleanup for orphaned deployment {deployment.id} "
                    f"(workspace {deployment.workspace_id}, state: {deployment.state})"
                )
                job_key = await run_destroy_compose(str(deployment.id))

                # Set task_run_id to prevent duplicate triggers
                deployment_check.current_task_run_id = job_key
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


async def cleanup_stranded_depot_projects_job(ctx: Context) -> dict[str, Any]:
    """Retry Depot project deletion for DELETED deployments that still have a depot_project_id.

    This handles cases where the Depot API call failed during deployment deletion.
    The depot_project_id is only cleared on successful deletion, so any DELETED
    deployment with a non-NULL depot_project_id needs cleanup.

    Args:
        ctx: SAQ job context

    Returns:
        Result dict with cleanup stats
    """
    from backend.services import get_depot_service

    logger.info("Starting cleanup of stranded Depot projects")

    async with get_db_context() as db:
        pending_cleanup = await db.compose_deployments.find_pending_depot_cleanup()

    if not pending_cleanup:
        logger.info("No stranded Depot projects found")
        return {
            "found": 0,
            "cleaned": 0,
            "errors": 0,
        }

    logger.info(f"Found {len(pending_cleanup)} deployments with pending Depot cleanup")

    depot_service = get_depot_service()
    cleaned = 0
    errors = 0

    for deployment in pending_cleanup:
        try:
            project_id = deployment.depot_project_id
            if not project_id:
                continue

            logger.info(
                f"Retrying Depot cleanup for deployment {deployment.id} "
                f"(project_id: {project_id})"
            )

            success = await depot_service.delete_project(project_id)

            if success:
                async with get_db_context() as db:
                    deployment_locked = await db.compose_deployments.get_by_id(
                        deployment.id, with_lock=True, include_deleted=True
                    )
                    if deployment_locked:
                        deployment_locked.depot_project_id = None
                        await db.compose_deployments.update(deployment_locked)

                logger.info(
                    f"Successfully cleaned up Depot project {project_id} "
                    f"for deployment {deployment.id}"
                )
                cleaned += 1
            else:
                logger.warning(
                    f"Failed to delete Depot project {project_id} "
                    f"for deployment {deployment.id}"
                )
                errors += 1

        except Exception as e:
            logger.warning(
                f"Error cleaning up Depot project for deployment {deployment.id}: {e}"
            )
            errors += 1

    result = {
        "found": len(pending_cleanup),
        "cleaned": cleaned,
        "errors": errors,
    }

    logger.info(
        f"Depot cleanup complete: found {result['found']}, "
        f"cleaned {result['cleaned']}, errors {result['errors']}"
    )

    return result
