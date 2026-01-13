"""Utility functions for deployment tasks."""

import asyncio
import time

from kubernetes_asyncio.client.exceptions import ApiException
from loguru import logger
from models.deployments import DeploymentStates

from backend.config import app_config
from backend.database import get_db_context
from backend.services import get_subscription_service
from backend.services.k8s.client import get_async_batch_v1_api


async def delete_job_with_timeout(
    job_name: str, namespace: str, timeout_seconds: int
) -> None:
    """Delete a Kubernetes Job and wait for it to be fully removed."""
    batch_v1 = await get_async_batch_v1_api()

    # Initiate deletion
    try:
        await asyncio.wait_for(
            batch_v1.delete_namespaced_job(
                name=job_name,
                namespace=namespace,
                propagation_policy="Foreground",
            ),
            timeout=timeout_seconds,
        )

    except ApiException as e:
        if e.status == 404:
            logger.debug(f"Job {job_name} does not exist (already deleted)")
            return
        raise

    except asyncio.TimeoutError:
        logger.error(
            f"Timeout ({timeout_seconds}s) initiating deletion of Job {job_name}"
        )
        raise TimeoutError(
            f"Job {job_name} deletion initiation timed out after {timeout_seconds} seconds"
        ) from None

    # Wait for Job to be fully deleted (poll until 404)
    start_time = time.time()
    poll_interval = 0.5
    while time.time() - start_time < timeout_seconds:
        try:
            await batch_v1.read_namespaced_job(name=job_name, namespace=namespace)
            # Job still exists, wait and retry
            await asyncio.sleep(poll_interval)

        except ApiException as e:
            if e.status == 404:
                logger.info(f"Job {job_name} successfully deleted and removed")
                return
            logger.warning(f"Error checking Job {job_name} deletion status: {e}")
            await asyncio.sleep(poll_interval)

        except Exception as e:
            logger.warning(f"Error checking Job {job_name} deletion status: {e}")
            await asyncio.sleep(poll_interval)

    # Timeout waiting for deletion
    logger.error(
        f"Timeout ({timeout_seconds}s) waiting for Job {job_name} to be fully deleted. "
        "Job deletion may still be in progress."
    )
    raise TimeoutError(
        f"Job {job_name} deletion timed out after {timeout_seconds} seconds"
    ) from None


async def update_deployment_state(
    deployment_id: str,
    state: DeploymentStates,
    message: str | None = None,
) -> None:
    """Update deployment status in database."""
    try:
        async with get_db_context() as db:
            deployment = await db.compose_deployments.get_by_id(deployment_id)

            if deployment:
                deployment.state = state
                if message:
                    deployment.status_message = message[:500]

                # Clear task ID when reaching terminal states
                if state in (
                    DeploymentStates.DEPLOYED,
                    DeploymentStates.FAILED,
                    DeploymentStates.DELETED,
                ):
                    deployment.current_task_run_id = None

                await db.compose_deployments.update(deployment)

    except Exception as e:
        logger.error(f"Failed to update deployment status: {e}")


async def verify_quota_capacity(
    workspace_id: str,
    required_deployments: int,
    required_services: int,
    required_pvcs: int,
    existing_deployments: int = 0,
    existing_services: int = 0,
    existing_pvcs: int = 0,
) -> None:
    """Verify subscription limits have capacity for required resources.

    Raises ValueError if insufficient capacity.

    Checks against subscription features, not Kubernetes quotas. Kubernetes quotas are set higher
    as a safety net, but real enforcement happens here.

    When updating an existing deployment, pass existing_* parameters to account for resources
    being replaced rather than added.
    """
    async with get_db_context() as db:
        owner_user = await db.workspaces.get_owner_user(workspace_id)

    if not owner_user:
        logger.warning(f"Workspace {workspace_id} has no owner, skipping quota check")
        return

    subscription_service = get_subscription_service()
    features = await subscription_service.get_user_features(owner_user.workos_id)

    # Get current usage from database
    async with get_db_context() as db:
        deployments = await db.compose_deployments.find(
            {"workspace_id": workspace_id, "deleted_at": None}
        )

    current_deployments_count = len(
        [d for d in deployments if d.state != DeploymentStates.DELETED]
    )

    # Calculate current services and volumes usage
    current_services_count = 0
    current_pvcs_count = 0
    for deployment in deployments:
        if deployment.state == DeploymentStates.DELETED:
            continue
        if deployment.helm_values and deployment.helm_values.services:
            current_services_count += len(
                [s for s in deployment.helm_values.services if s.enabled]
            )
        if deployment.helm_values and deployment.helm_values.volumes:
            current_pvcs_count += len(deployment.helm_values.volumes)

    # Subtract existing resources being replaced
    effective_deployments_used = current_deployments_count - existing_deployments
    effective_services_used = current_services_count - existing_services
    effective_pvcs_used = current_pvcs_count - existing_pvcs

    # Calculate limits from subscription features
    deployments_limit = features.workspace.deployment_limit
    services_limit = (
        features.workspace.deployment_limit * features.deployment.service_limit
    )
    pvcs_limit = features.workspace.deployment_limit * features.deployment.volume_limit

    # Each compose deployment counts as 1 deployment, regardless of services
    required_deployments_count = 1

    errors = []

    if effective_deployments_used + required_deployments_count > deployments_limit:
        available = max(0, deployments_limit - effective_deployments_used)
        errors.append(
            f"Deployment limit exceeded. Your plan allows {deployments_limit} deployment(s) per workspace, "
            f"but this would require {effective_deployments_used + required_deployments_count}. "
            f"{available} deployment slot(s) available."
        )

    if effective_services_used + required_services > services_limit:
        available = max(0, services_limit - effective_services_used)
        errors.append(
            f"Service limit exceeded. Your plan allows {services_limit} service(s) per workspace, "
            f"but this deployment would require {required_services} service(s). "
            f"{available} service slot(s) available."
        )

    if effective_pvcs_used + required_pvcs > pvcs_limit:
        available = max(0, pvcs_limit - effective_pvcs_used)
        errors.append(
            f"Volume limit exceeded. Your plan allows {pvcs_limit} volume(s) per workspace, "
            f"but this deployment would require {required_pvcs} volume(s). "
            f"{available} volume slot(s) available."
        )

    if errors:
        error_msg = f"{' '.join(errors)} Please reduce the number of resources or upgrade your plan."
        raise ValueError(error_msg)


async def wait_for_secrets(deployment_id: str, timeout: int | None = None) -> None:
    """Wait for secrets to be stored for a deployment."""
    if timeout is None:
        timeout = app_config.SECRETS_TIMEOUT_SECONDS

    start_time = time.time()
    secrets = []
    while time.time() - start_time < timeout:
        await asyncio.sleep(1)
        async with get_db_context() as db:
            secrets = await db.secrets.get_secrets(deployment_id)

        if secrets:
            logger.info(f"Found {len(secrets)} secrets for deployment {deployment_id}")
            break

    if not secrets:
        raise TimeoutError(
            f"Secrets not found for deployment {deployment_id} after {timeout} seconds"
        )
