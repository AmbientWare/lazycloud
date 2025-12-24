from datetime import UTC, datetime

import yaml
from loguru import logger
from models.deployments import DeploymentStates
from prefect import task

from backend.database import get_db_context
from backend.prefect_app.deployment.tasks import unregister_custom_domains_task
from backend.prefect_app.deployment.utils import update_deployment_state
from backend.services import get_depot_service, get_ecr_auth_service
from backend.services.compose.parser import ComposeParser
from backend.services.k8s import create_release_name
from backend.services.k8s.helm_manager import HelmManager


@task(log_prints=True)
async def destroy_compose_task(deployment_id: str) -> None:
    """Destroy a Docker Compose deployment from Kubernetes."""
    logger.info(f"Starting destruction of deployment {deployment_id}")
    ecr_auth_service = get_ecr_auth_service()

    # get the deployment (include_deleted=True to access soft-deleted deployments)
    async with get_db_context() as db:
        deployment = await db.compose_deployments.get_by_id(
            deployment_id, include_deleted=True
        )

    if not deployment:
        raise Exception(f"Deployment {deployment_id} not found")

    # Update status to deleting
    await update_deployment_state(
        deployment_id, DeploymentStates.DELETING, "Starting deletion"
    )

    # Step 0: Unregister custom domains from Cloudflare (non-fatal)
    compose_yaml = deployment.compose_yaml or deployment.pending_compose_yaml
    if compose_yaml:
        try:
            compose_data = yaml.safe_load(compose_yaml)
            compose_file = ComposeParser.parse_dict(compose_data)
            custom_domains = [
                service.domain for service in compose_file.services if service.domain
            ]
            if custom_domains:
                await unregister_custom_domains_task(custom_domains)

        except Exception as cf_error:
            logger.warning(f"Cloudflare domain cleanup failed (continuing): {cf_error}")

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

        # Step 3.5: Clean up Depot project (non-fatal)
        logger.info(f"Cleaning up Depot project for deployment {deployment_id}")
        try:
            depot_service = get_depot_service()
            # Pass deployment object to avoid redundant DB query
            await depot_service.delete_deployment_project(deployment=deployment)

        except Exception as depot_error:
            # Depot cleanup is non-fatal - log warning but continue
            logger.warning(f"Depot cleanup failed (continuing): {depot_error}")

        # Step 4: Update deployment state to DELETED and set deleted_at
        # Re-fetch deployment to ensure we have latest state (may have been updated)

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
