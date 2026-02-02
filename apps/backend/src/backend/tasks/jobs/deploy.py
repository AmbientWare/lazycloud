"""Deploy compose SAQ job - deploys Docker Compose to Kubernetes."""

from typing import Any

from loguru import logger
from models.deployments import DeploymentStates
from models.secrets import SecretState

from backend.database import get_db_context
from backend.services.k8s import create_release_name
from backend.services.k8s.helm_manager import HelmManager
from backend.tasks.core import (
    check_deployment_idempotency,
    delete_existing_jobs,
    deploy_application,
    deploy_namespace_resources,
    prepare_deployment,
    prepare_namespace_config,
    register_custom_domains,
    sync_deployment_to_db,
    update_deployment_state,
)


async def deploy_compose_job(
    ctx: dict[str, Any],
    deployment_id: str,
    wait_for_secrets: bool = False,
    service_names: list[str] | None = None,
) -> dict[str, Any]:
    """Deploy a Docker Compose file to Kubernetes.

    Args:
        ctx: SAQ job context
        deployment_id: The deployment ID to deploy
        wait_for_secrets: Whether to wait for secrets before deploying
        service_names: Optional list of specific services to deploy

    Returns:
        Result dict with status and deployment_id
    """
    services_str = ", ".join(service_names) if service_names else ""
    logger.info(
        f"Starting deployment job for {deployment_id}"
        + (f" (services: {services_str})" if service_names else "")
    )

    try:
        # Step 1: Check idempotency and update state
        deployment_info = await check_deployment_idempotency(deployment_id)
        if deployment_info is None:
            logger.info(f"Deployment {deployment_id} skipped (already deployed)")
            return {"status": "skipped", "reason": "already_deployed"}

        # Step 2: Prepare deployment data
        validation_result = await prepare_deployment(
            deployment_id, wait_for_secrets_flag=wait_for_secrets
        )
        deployment = validation_result.deployment
        helm_values = validation_result.helm_values
        secrets = validation_result.secrets

        # Step 3: Prepare namespace config
        namespace_config_result = await prepare_namespace_config(deployment)

        # Step 4: Deploy namespace resources
        await update_deployment_state(
            deployment_id, DeploymentStates.DEPLOYING, "Deploying namespace resources"
        )
        await deploy_namespace_resources(namespace_config_result, deployment.cluster_id)

        # Step 5: Delete existing jobs
        await update_deployment_state(
            deployment_id, DeploymentStates.DEPLOYING, "Cleaning up existing jobs"
        )
        await delete_existing_jobs(
            namespace=deployment.namespace,
            helm_values=helm_values,
            cluster_id=deployment.cluster_id,
            current_helm_values=deployment_info.current_helm_values
            if deployment_info
            else None,
        )

        # Step 6: Deploy application
        name = create_release_name(deployment.workspace_id, deployment.name)
        await update_deployment_state(
            deployment_id, DeploymentStates.DEPLOYING, "Deploying application"
        )
        deploy_result = await deploy_application(
            name, deployment.namespace, helm_values, deployment.cluster_id
        )

        # Step 7: Register custom domains with Cloudflare (non-blocking)
        custom_domains = (
            [
                service.domain
                for service in validation_result.compose_file.services
                if service.domain
            ]
            if validation_result.compose_file
            else []
        )
        if custom_domains:
            await register_custom_domains(custom_domains)

        # Step 8: Sync to database
        try:
            await sync_deployment_to_db(
                deployment_id,
                helm_values,
                deploy_result.revision,
                secrets,
            )

        except Exception as db_error:
            # Rollback Helm deployment if DB sync fails
            logger.error(
                f"Helm deployment succeeded but DB update failed for deployment {deployment_id}. "
                f"Helm is at revision {deploy_result.revision} but DB update failed: {db_error}. "
                "Attempting to rollback Helm deployment to maintain consistency..."
            )

            helm_manager = HelmManager(deployment.cluster_id)
            helm_revision = deploy_result.revision
            if helm_revision and helm_revision > 1:
                rollback_result = await helm_manager.rollback(
                    name, deployment.namespace, helm_revision - 1
                )
                if rollback_result.success:
                    logger.info(
                        f"Successfully rolled back Helm release {name} to revision {helm_revision - 1}"
                    )
                else:
                    logger.warning(
                        f"Failed to rollback Helm release {name}: {rollback_result.error}"
                    )

            else:
                logger.info(
                    f"Fresh install detected (revision {helm_revision}), destroying release instead of rolling back"
                )
                destroy_result = await helm_manager.destroy(name, deployment.namespace)

                if destroy_result.success:
                    logger.info(
                        f"Successfully destroyed Helm release {name} after DB update failure"
                    )
                else:
                    logger.warning(
                        f"Failed to destroy Helm release {name}: {destroy_result.error}"
                    )

            await update_deployment_state(
                deployment_id,
                DeploymentStates.FAILED,
                f"Deployment partially completed: Helm deployed successfully (revision: {helm_revision}) but database update failed. "
                f"Helm rollback attempted. Error: {str(db_error)[:200]}",
            )

            raise ValueError(
                f"Helm deployment succeeded but database update failed: {db_error}"
            ) from db_error

        logger.info(f"Successfully completed deployment job for {deployment_id}")
        return {"status": "success", "deployment_id": deployment_id}

    except Exception as e:
        error_type = type(e).__name__
        logger.error(
            f"Deployment job {deployment_id} failed with {error_type}: {str(e)}",
            exc_info=True,
        )

        # Convert deployment errors to simple user-friendly messages
        error_str = str(e).lower()
        if "timeout" in error_str:
            user_message = "Deployment timed out. Please try again."
        else:
            user_message = "Deployment failed. Please try again or contact support if the issue persists."

        # Update state to failed
        try:
            await update_deployment_state(
                deployment_id,
                DeploymentStates.FAILED,
                user_message,
            )

        except Exception as state_error:
            logger.error(
                f"Failed to update deployment state after failure: {state_error}",
                exc_info=True,
            )

        # Reset secrets state if we have them
        try:
            async with get_db_context() as db:
                secrets_list = await db.secrets.get_secrets(deployment_id)

                if secrets_list:
                    for secret in secrets_list:
                        secret.state = SecretState.AWAITING_DEPLOYMENT
                        await db.secrets.update(secret)

        except Exception as secret_error:
            logger.warning(f"Failed to reset secrets state: {secret_error}")

        raise
