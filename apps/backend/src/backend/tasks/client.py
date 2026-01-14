"""SAQ client for triggering background jobs from API endpoints."""

from typing import Any
from uuid import uuid4

from loguru import logger

from backend.tasks.queue import get_background_queue


class JobNames:
    """Job name constants for background tasks."""

    DEPLOY_COMPOSE = "deploy_compose_job"
    DESTROY_COMPOSE = "destroy_compose_job"
    ROLLBACK_COMPOSE = "rollback_compose_job"
    DELETE_INSTANCE = "delete_instance_job"
    RESTART_SERVICE = "restart_service_job"
    RESTART_ALL_SERVICES = "restart_all_services_job"


async def enqueue_job(
    job_name: str,
    kwargs: dict[str, Any],
    timeout: int = 600,
    retries: int = 1,
) -> str:
    """Enqueue a background job and return the job key.

    Args:
        job_name: Name of the job function to run
        kwargs: Arguments to pass to the job
        timeout: Job timeout in seconds (default 10 minutes)
        retries: Number of retry attempts on failure

    Returns:
        Job key (UUID string) for status tracking
    """
    queue = get_background_queue()

    # Generate a unique job key
    job_key = str(uuid4())

    job = await queue.enqueue(
        job_name,
        key=job_key,
        timeout=timeout,
        retries=retries,
        **kwargs,
    )

    if job is None:
        # Job already exists with this key (shouldn't happen with uuid4)
        logger.warning(f"Job {job_name} with key {job_key} already enqueued")
        return job_key

    logger.info(
        f"Enqueued job {job_name} with key {job_key}, parameters: {list(kwargs.keys())}"
    )

    return job_key


# Convenience functions for each job type


async def run_deploy_compose(
    deployment_id: str,
    wait_for_secrets: bool = False,
    service_names: list[str] | None = None,
) -> str:
    """Enqueue a deploy compose job.

    Args:
        deployment_id: The deployment ID to deploy
        wait_for_secrets: Whether to wait for secrets before deploying
        service_names: Optional list of specific services to deploy

    Returns:
        Job key for status tracking
    """
    return await enqueue_job(
        JobNames.DEPLOY_COMPOSE,
        {
            "deployment_id": deployment_id,
            "wait_for_secrets": wait_for_secrets,
            "service_names": service_names,
        },
        timeout=900,  # 15 minute timeout for deployments
        retries=0,  # No automatic retries for deployments
    )


async def run_destroy_compose(deployment_id: str) -> str:
    """Enqueue a destroy compose job.

    Args:
        deployment_id: The deployment ID to destroy

    Returns:
        Job key for status tracking
    """
    return await enqueue_job(
        JobNames.DESTROY_COMPOSE,
        {"deployment_id": deployment_id},
        timeout=600,  # 10 minute timeout
        retries=0,
    )


async def run_rollback_compose(deployment_id: str, revision: int) -> str:
    """Enqueue a rollback compose job.

    Args:
        deployment_id: The deployment ID to rollback
        revision: The target Helm revision to rollback to

    Returns:
        Job key for status tracking
    """
    return await enqueue_job(
        JobNames.ROLLBACK_COMPOSE,
        {"deployment_id": deployment_id, "revision": revision},
        timeout=600,
        retries=0,
    )


async def run_delete_instance(
    deployment_id: str,
    service_name: str,
    pod_name: str,
    force: bool = False,
) -> str:
    """Enqueue a delete instance job.

    Args:
        deployment_id: The deployment ID
        service_name: The service name containing the instance
        pod_name: The pod/instance name to delete
        force: Whether to force delete (no grace period)

    Returns:
        Job key for status tracking
    """
    return await enqueue_job(
        JobNames.DELETE_INSTANCE,
        {
            "deployment_id": deployment_id,
            "service_name": service_name,
            "pod_name": pod_name,
            "force": force,
        },
        timeout=120,  # 2 minute timeout
        retries=0,
    )


async def run_restart_service(deployment_id: str, service_name: str) -> str:
    """Enqueue a restart service job.

    Args:
        deployment_id: The deployment ID
        service_name: The service name to restart

    Returns:
        Job key for status tracking
    """
    return await enqueue_job(
        JobNames.RESTART_SERVICE,
        {"deployment_id": deployment_id, "service_name": service_name},
        timeout=300,  # 5 minute timeout
        retries=0,
    )


async def run_restart_all_services(deployment_id: str) -> str:
    """Enqueue a restart all services job.

    Args:
        deployment_id: The deployment ID

    Returns:
        Job key for status tracking
    """
    return await enqueue_job(
        JobNames.RESTART_ALL_SERVICES,
        {"deployment_id": deployment_id},
        timeout=600,  # 10 minute timeout
        retries=0,
    )
