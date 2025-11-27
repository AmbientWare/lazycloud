from loguru import logger
from prefect import flow

from lazycloud_api.config import app_config
from lazycloud_api.database import get_db_context
from lazycloud_api.services.k8s import create_release_name
from lazycloud_api.services.k8s.helm_manager import HelmManager
from shared.models.deployments import DeploymentStates


@flow(log_prints=True)
async def reconcile_rollback_states(
    minutes_old: int | None = None,
) -> dict:
    """Reconcile Helm and database states for stuck deployments."""
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

    helm_manager = HelmManager()

    for deployment in stuck_deployments:
        try:
            name = create_release_name(deployment.workspace_id, deployment.name)
            namespace = deployment.namespace

            if not name:
                logger.warning(f"Skipping deployment {deployment.id}: no release name")
                skipped += 1
                continue

            helm_revision = helm_manager._get_latest_revision(name, namespace)
            db_revision = deployment.current_helm_revision

            if helm_revision is None:
                logger.warning(
                    f"Deployment {deployment.id} ({name}): Helm release not found. "
                    "May have been deleted externally."
                )
                skipped += 1
                continue

            history = helm_manager.get_history(name, namespace)
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


reconcile_rollback_states_deployment = reconcile_rollback_states.to_deployment(
    name="reconcile-rollback-states",
    cron=f"*/{app_config.ROLLBACK_RECONCILIATION_INTERVAL_MINUTES} * * * *",
)
