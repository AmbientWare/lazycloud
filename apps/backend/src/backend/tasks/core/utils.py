"""Utility functions for deployment tasks."""

import asyncio
import time

from kubernetes_asyncio.client.exceptions import ApiException
from loguru import logger
from models.deployments import DeploymentStates

from models.users import UserRole

from backend.config import app_config
from backend.database import get_db_context
from backend.services import get_subscription_service
from backend.services.k8s.client import get_async_batch_v1_api


async def delete_job_with_timeout(
    job_name: str,
    namespace: str,
    timeout_seconds: int,
    cluster_id: str,
) -> None:
    """Delete a Kubernetes Job and wait for it to be fully removed."""
    batch_v1 = await get_async_batch_v1_api(cluster_id)

    # Initiate deletion
    try:
        await asyncio.wait_for(
            batch_v1.delete_namespaced_job(  # type: ignore
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
            await batch_v1.read_namespaced_job(name=job_name, namespace=namespace)  # type: ignore
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
    is_update: bool = False,
) -> None:
    """Verify subscription limits have capacity for a new deployment.

    Raises ValueError if insufficient capacity.

    Checks total deployment limit across ALL workspaces owned by the user.
    Services, volumes, and networks per deployment are NOT limited (usage billing handles cost).

    Args:
        workspace_id: The workspace to check quota for
        is_update: If True, this is an update to an existing deployment (doesn't count against limit)
    """
    async with get_db_context() as db:
        owner_user = await db.workspaces.get_owner_user(workspace_id)

    if not owner_user:
        logger.warning(f"Workspace {workspace_id} has no owner, skipping quota check")
        return

    if owner_user.role == UserRole.ADMIN:
        return

    subscription_service = get_subscription_service()
    features = await subscription_service.get_user_features(owner_user.workos_id)

    # Get total deployment count across ALL user's workspaces
    async with get_db_context() as db:
        total_deployments = (
            await db.compose_deployments.get_total_deployment_count_for_user(
                owner_user.id
            )
        )

    # If updating existing deployment, it doesn't count as a new one
    if is_update:
        return

    # Check deployment limit for new deployments
    if total_deployments >= features.deployment_limit:
        error_msg = (
            f"Deployment limit exceeded. Your plan allows {features.deployment_limit} deployment(s), "
            f"you currently have {total_deployments}. "
            "Please upgrade your plan to create more deployments."
        )
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
