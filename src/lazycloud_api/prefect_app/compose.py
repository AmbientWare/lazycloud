import asyncio
import time
from datetime import UTC, datetime

import yaml
from kubernetes.client.exceptions import ApiException
from loguru import logger
from prefect import task

from lazycloud_api.config import app_config
from lazycloud_api.database import db
from lazycloud_api.services import get_ecr_auth_service
from lazycloud_api.services.compose.parser import ComposeParser
from lazycloud_api.services.k8s import (
    create_release_name,
    get_chart_paths,
)
from lazycloud_api.services.k8s.client import get_batch_v1_api
from lazycloud_api.services.k8s.helm_manager import (
    DeploymentStrategy,
    HelmDeploymentConfig,
    HelmManager,
)
from lazycloud_api.services.k8s.helm_values_generator import HelmValuesGenerator
from shared.models.deployments import DeploymentStates
from shared.models.helm import HelmNamespaceValues, NamespaceConfig
from shared.models.k8s import WorkloadType
from shared.models.secrets import SecretState

charts = get_chart_paths()


async def _update_deployment_state(
    deployment_id: str,
    state: DeploymentStates,
    message: str | None = None,
) -> None:
    """Update deployment status in database."""
    try:
        deployment = await db.compose_deployments.aget_by_id(deployment_id)
        if deployment:
            deployment.state = state
            if message:
                deployment.status_message = message[:500]  # Truncate to fit
            await db.compose_deployments.aupdate(deployment)

    except Exception as e:
        logger.error(f"Failed to update deployment status: {e}")


async def _wait_for_secrets(deployment_id: str, timeout: int | None = None) -> None:
    """Wait for secrets to be stored for a deployment."""
    if timeout is None:
        timeout = app_config.SECRETS_TIMEOUT_SECONDS

    start_time = time.time()
    secrets = []
    while time.time() - start_time < timeout:
        await asyncio.sleep(1)
        secrets = await db.secrets.aget_secrets(deployment_id)
        if secrets:
            logger.info(f"Found {len(secrets)} secrets for deployment {deployment_id}")
            break

    if not secrets:
        raise TimeoutError(
            f"Secrets not found for deployment {deployment_id} after {timeout} seconds"
        )


@task
async def deploy_compose_task(
    deployment_id: str, wait_for_secrets: bool = False
) -> None:
    """Deploy a Docker Compose file to Kubernetes using improved Helm management."""
    logger.info(f"Starting deployment {deployment_id}")
    secrets = []

    if wait_for_secrets:
        try:
            await _wait_for_secrets(
                deployment_id, timeout=app_config.SECRETS_TIMEOUT_SECONDS
            )
        except TimeoutError as e:
            logger.error(f"Timeout waiting for secrets: {e}")
            await _update_deployment_state(
                deployment_id,
                DeploymentStates.FAILED,
                f"Timeout waiting for secrets: {str(e)}",
            )
            raise

    # get deployment
    deployment = await db.compose_deployments.aget_by_id(deployment_id)
    if not deployment:
        raise ValueError(f"Deployment {deployment_id} not found")

    # Parse the compose YAML (use pending if available, otherwise use current)
    compose_yaml = deployment.pending_compose_yaml or deployment.compose_yaml
    try:
        compose_data = yaml.safe_load(compose_yaml)
    except yaml.YAMLError as e:
        logger.error(f"YAML parsing error for deployment {deployment_id}: {e}")
        await _update_deployment_state(
            deployment_id,
            DeploymentStates.FAILED,
            f"Invalid compose YAML: {str(e)}",
        )
        raise ValueError(f"Invalid YAML in deployment: {e}") from e

    compose_file = ComposeParser.parse_dict(compose_data)

    # get the helm values with deployment_id to load secrets
    secrets = await db.secrets.aget_secrets(deployment_id)
    helm_generator = HelmValuesGenerator(deployment, secrets)
    helm_values, _ = helm_generator.generate_values(compose_file)
    deployment.helm_values = helm_values

    # Update status to deploying
    await _update_deployment_state(
        deployment_id,
        DeploymentStates.DEPLOYING,
        "Starting deployment",
    )

    # Extract deployment info from helm values
    name = create_release_name(deployment.workspace_id, deployment.name)
    namespace = deployment.namespace

    # Initialize Helm manager
    helm_manager = HelmManager()

    try:
        # Deploy namespace resources (NetworkPolicy, ResourceQuota, etc.)
        logger.info(f"Deploying namespace resources for {namespace}")
        namespace_values = HelmNamespaceValues(
            namespace=NamespaceConfig(
                name=namespace,
                labels={
                    "lazycloud.io/managed": "true",
                    "lazycloud.io/workspace-id": str(deployment.workspace_id),
                },
            )
        )
        namespace_config = HelmDeploymentConfig(
            release_name=namespace,
            namespace="default",
            chart_path=str(charts.namespace),
            values=namespace_values,
            timeout="2m",
            wait=True,
        )

        namespace_result = helm_manager.deploy(namespace_config)
        if not namespace_result.success:
            if (
                namespace_result.error
                and "already exists" not in namespace_result.error
            ):
                raise Exception(f"Failed to deploy namespace: {namespace_result.error}")

        # Step 2: Deploy application
        logger.info(f"Deploying application {name} in namespace {namespace}")

        # Check if deployment contains Jobs - Jobs have immutable spec.template and must be deleted before upgrade
        job_services = [
            service
            for service in helm_values.services
            if service.enabled and service.workloadType == WorkloadType.JOB
        ]

        # Delete existing Jobs before upgrade (they can't be patched)
        if job_services:
            logger.info(
                f"Deployment contains {len(job_services)} Job(s), deleting existing Jobs before upgrade"
            )
            batch_v1 = get_batch_v1_api()
            for service in job_services:
                try:
                    batch_v1.delete_namespaced_job(
                        name=service.resourceName,
                        namespace=namespace,
                        propagation_policy="Foreground",
                    )
                    logger.info(f"Deleted existing Job: {service.name}")
                except ApiException as e:
                    # Job might not exist (first deployment), that's okay
                    if e.status != 404:
                        logger.warning(f"Could not delete Job {service.name}: {e}")
                except Exception as e:
                    logger.warning(f"Unexpected error deleting Job {service.name}: {e}")

        helm_app_config = HelmDeploymentConfig(
            release_name=name,
            namespace=namespace,
            chart_path=str(charts.compose),
            values=deployment.helm_values,
            timeout="5m",
            strategy=DeploymentStrategy.ROLLING_UPDATE,
        )

        app_result = helm_manager.deploy(helm_app_config)
        if not app_result.success:
            # Check if it's a stuck deployment issue
            if app_result.error and (
                "another operation" in app_result.error or "pending" in app_result.error
            ):
                logger.warning("Detected stuck deployment, attempting force update")
                helm_app_config.strategy = DeploymentStrategy.FORCE_UPDATE
                app_result = helm_manager.deploy(helm_app_config)

            if not app_result.success:
                raise Exception(f"Failed to deploy application: {app_result.error}")

        # Promote pending_compose_yaml to compose_yaml after successful deployment
        if deployment.pending_compose_yaml:
            deployment.compose_yaml = deployment.pending_compose_yaml
            deployment.pending_compose_yaml = None

        # Update state, message, and timestamp in one go
        deployment.state = DeploymentStates.DEPLOYED
        deployment.status_message = (
            f"Deployment initiated successfully (revision: {app_result.revision})"
        )
        deployment.deployed_at = datetime.now(UTC)

        # Single database update with all changes
        await db.compose_deployments.aupdate(deployment)

        # update the secrets state to deployed
        if secrets:
            async with db.secrets.transaction() as session:
                for secret in secrets:
                    secret.state = SecretState.DEPLOYED
                    await db.secrets.aupdate(secret, session=session)

    except yaml.YAMLError as e:
        logger.error(f"YAML parsing error for deployment {deployment_id}: {e}")
        await _update_deployment_state(
            deployment_id,
            DeploymentStates.FAILED,
            f"Invalid compose YAML: {str(e)}",
        )
        raise ValueError(f"Invalid YAML in deployment: {e}") from e
    except ValueError as e:
        error_type = type(e).__name__
        logger.error(
            f"Deployment {deployment_id} failed with {error_type}: {str(e)}",
            exc_info=True,
        )
        await _update_deployment_state(
            deployment_id,
            DeploymentStates.FAILED,
            f"Deployment failed: {error_type} - {str(e)[:200]}",
        )
        if secrets:
            async with db.secrets.transaction() as session:
                for secret in secrets:
                    secret.state = SecretState.AWAITING_DEPLOYMENT
                    await db.secrets.aupdate(secret, session=session)
        raise
    except Exception as e:
        error_type = type(e).__name__
        logger.error(
            f"Deployment {deployment_id} failed with {error_type}: {str(e)}",
            exc_info=True,
        )
        await _update_deployment_state(
            deployment_id,
            DeploymentStates.FAILED,
            f"Deployment failed: {error_type} - {str(e)[:200]}",
        )

        # update the secrets state back to awaiting deployment on failure
        if secrets:
            async with db.secrets.transaction() as session:
                for secret in secrets:
                    secret.state = SecretState.AWAITING_DEPLOYMENT
                    await db.secrets.aupdate(secret, session=session)

        # Attempt cleanup on failure
        try:
            if name and namespace and helm_manager:
                logger.info(
                    f"Attempting to cleanup failed deployment {name} in namespace {namespace}"
                )
                helm_manager.destroy(name, namespace)
        except Exception as cleanup_error:
            logger.error(
                f"Cleanup failed for deployment {deployment_id}: {cleanup_error}"
            )

        raise


@task
async def destroy_compose_task(deployment_id: str) -> None:
    """Destroy a Docker Compose deployment from Kubernetes."""
    logger.info(f"Starting destruction of deployment {deployment_id}")
    ecr_auth_service = get_ecr_auth_service()

    # get the deployment
    deployment = await db.compose_deployments.aget_by_id(deployment_id)
    if not deployment:
        raise Exception(f"Deployment {deployment_id} not found")

    # Update status to deleting
    await _update_deployment_state(
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
                str(deployment.workspace_id), deployment.name
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
        deployment = await db.compose_deployments.aget_by_id(deployment_id)
        if deployment:
            deployment.deleted_at = datetime.now(UTC)
            deployment.state = DeploymentStates.DELETED
            deployment.status_message = "Deployment deleted"
            await db.compose_deployments.aupdate(deployment)

        logger.info(f"Successfully destroyed deployment {deployment_id}")

    except Exception as e:
        error_type = type(e).__name__
        logger.error(
            f"Destruction of deployment {deployment_id} failed with {error_type}: {str(e)}",
            exc_info=True,
        )
        await _update_deployment_state(
            deployment_id,
            DeploymentStates.FAILED,
            f"Deletion failed: {error_type} - {str(e)[:200]}",
        )
        raise
