"""Destroy compose SAQ job - removes Docker Compose deployment from Kubernetes."""

from datetime import UTC, datetime
from typing import Any

from loguru import logger
from models.deployments import DeploymentStates
from saq.types import Context

from backend.database import get_db_context
from backend.services import get_depot_service
from backend.services.k8s import create_release_name
from backend.services.k8s.helm_manager import HelmManager
from backend.tasks.core import (
    reconcile_custom_domains_for_deployment,
    update_deployment_state,
)


async def destroy_compose_job(
    ctx: Context,
    deployment_id: str,
) -> dict[str, Any]:
    """Destroy a Docker Compose deployment from Kubernetes.

    Args:
        ctx: SAQ job context
        deployment_id: The deployment ID to destroy

    Returns:
        Result dict with status
    """
    logger.info(f"Starting destruction of deployment {deployment_id}")

    # Get the deployment (include_deleted=True to access soft-deleted deployments)
    async with get_db_context() as db:
        deployment = await db.compose_deployments.get_by_id(
            deployment_id, include_deleted=True
        )

    if not deployment:
        raise ValueError(f"Deployment {deployment_id} not found")

    # Update status to deleting
    await update_deployment_state(
        deployment_id, DeploymentStates.DELETING, "Starting deletion"
    )

    # Step 0: Unregister custom domains from Cloudflare (non-fatal)
    compose_yaml = deployment.compose_yaml or deployment.pending_compose_yaml
    if compose_yaml:
        try:
            removed_domains, skipped_domains = (
                await reconcile_custom_domains_for_deployment(
                    deployment_id=deployment_id,
                    previous_compose_yaml=compose_yaml,
                    current_compose_file=None,
                )
            )
            if removed_domains:
                logger.info(
                    f"Removed custom domains during destroy for deployment {deployment_id}: "
                    f"{', '.join(removed_domains)}"
                )
            if skipped_domains:
                logger.info(
                    f"Skipped custom domain removal during destroy for deployment {deployment_id} "
                    f"(still referenced): {', '.join(skipped_domains)}"
                )

        except Exception as cf_error:
            logger.warning(f"Cloudflare domain cleanup failed (continuing): {cf_error}")

    # Initialize Helm manager with cluster_id
    helm_manager = HelmManager(deployment.cluster_id)
    name = create_release_name(deployment.workspace_id, deployment.name)
    namespace = deployment.namespace

    if not name:
        raise ValueError("Deployment name is required")

    try:
        # Step 1: Destroy application
        logger.info(f"Destroying application {name} in namespace {namespace}")
        app_result = await helm_manager.destroy(name, namespace)
        if not app_result.success:
            logger.warning(f"Failed to destroy application: {app_result.error}")

        # Step 2: Destroy namespace resources only if no other active deployments remain
        async with get_db_context() as db:
            remaining = await db.compose_deployments.get_active_deployments_for_workspace(
                str(deployment.workspace_id)
            )
            # Exclude the current deployment being destroyed
            remaining.pop(deployment.name, None)

        if not remaining:
            logger.info(f"Last deployment in workspace, destroying namespace resources for {namespace}")
            namespace_result = await helm_manager.destroy(namespace, "default")
            if not namespace_result.success:
                logger.warning(
                    f"Failed to destroy namespace resources: {namespace_result.error}"
                )
        else:
            logger.info(
                f"Skipping namespace destruction for {namespace}, "
                f"{len(remaining)} other deployment(s) still active: {list(remaining.keys())}"
            )

        # Step 3: Clean up Depot project (non-fatal)
        logger.info(f"Cleaning up Depot project for deployment {deployment_id}")
        try:
            depot_service = get_depot_service()
            # Pass deployment object to avoid redundant DB query
            await depot_service.delete_deployment_project(deployment=deployment)

        except Exception as depot_error:
            # Depot cleanup is non-fatal - log warning but continue
            logger.warning(f"Depot cleanup failed (continuing): {depot_error}")

        # Step 4: Update deployment state to DELETED and set deleted_at
        async with get_db_context() as db:
            deployment = await db.compose_deployments.get_by_id(
                deployment_id, include_deleted=True
            )

        if deployment:
            # Set deleted_at if not already set (workspace deletion sets it earlier)
            if not deployment.deleted_at:
                deployment.deleted_at = datetime.now(UTC)

            deployment.state = DeploymentStates.DELETED
            deployment.status_message = "Deployment deleted"
            deployment.current_task_run_id = None

            async with get_db_context() as db:
                await db.compose_deployments.update(deployment)

        logger.info(f"Successfully destroyed deployment {deployment_id}")
        return {"status": "success", "deployment_id": deployment_id}

    except Exception as e:
        error_type = type(e).__name__
        logger.error(
            f"Destruction of deployment {deployment_id} failed with {error_type}: {str(e)}",
            exc_info=True,
        )

        await update_deployment_state(
            deployment_id,
            DeploymentStates.FAILED,
            f"Deletion failed: {error_type} - {str(e)[:200]}",
        )
        raise
