from datetime import UTC, datetime

from loguru import logger
from prefect import task

from lazycloud_api.database import db
from lazycloud_api.prefect_app.deployment.utils import update_deployment_state
from lazycloud_api.services import get_ecr_auth_service
from lazycloud_api.services.k8s import create_release_name
from lazycloud_api.services.k8s.helm_manager import HelmManager
from shared.models.deployments import DeploymentStates


@task(log_prints=True)
async def destroy_compose_task(deployment_id: str) -> None:
    """Destroy a Docker Compose deployment from Kubernetes."""
    logger.info(f"Starting destruction of deployment {deployment_id}")
    ecr_auth_service = get_ecr_auth_service()

    # get the deployment
    deployment = await db.compose_deployments.get_by_id(deployment_id)
    if not deployment:
        raise Exception(f"Deployment {deployment_id} not found")

    # Update status to deleting
    await update_deployment_state(
        deployment_id, DeploymentStates.DELETING, "Starting deletion"
    )

    # Initialize Helm manager
    helm_manager = HelmManager()
    name = create_release_name(deployment.workspace_id, deployment.name)
    namespace = deployment.namespace

    if not name:
        raise Exception("Deployment name is required")

    try:
        # Step 1: Destroy application
        logger.info(f"Destroying application {name} in namespace {namespace}")
        app_result = helm_manager.destroy(name, namespace)
        if not app_result.success:
            logger.warning(f"Failed to destroy application: {app_result.error}")

        # Step 2: Destroy namespace resources (only if not default namespace)
        logger.info(f"Destroying namespace resources for {namespace}")
        namespace_result = helm_manager.destroy(namespace, "default")
        if not namespace_result.success:
            logger.warning(
                f"Failed to destroy namespace resources: {namespace_result.error}"
            )

        # Step 3: Clean up ECR repositories
        logger.info(f"Cleaning up ECR repositories for deployment {deployment.name}")
        try:
            deleted_repos = await ecr_auth_service.delete_deployment_repositories(
                deployment.workspace_id, deployment.name
            )

            if deleted_repos:
                logger.info(
                    f"Deleted {len(deleted_repos)} ECR repositories: {deleted_repos}"
                )

            else:
                logger.info("No ECR repositories found to delete")

        except Exception as ecr_error:
            # ECR cleanup is non-fatal - log warning but continue
            logger.warning(f"ECR cleanup failed (continuing): {ecr_error}")

        # Step 4: Soft delete deployment from database
        deployment = await db.compose_deployments.get_by_id(deployment_id)
        if deployment:
            deployment.deleted_at = datetime.now(UTC)
            deployment.state = DeploymentStates.DELETED
            deployment.status_message = "Deployment deleted"
            deployment.current_task_run_id = None
            await db.compose_deployments.update(deployment)

        logger.info(f"Successfully destroyed deployment {deployment_id}")

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
