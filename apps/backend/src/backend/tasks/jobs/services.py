"""Service management SAQ jobs - restart services."""

from typing import Any

from loguru import logger
from saq.types import Context

from backend.database import get_db_context
from backend.services.k8s.workload_manager import WorkloadManager


async def restart_service_job(
    ctx: Context,
    deployment_id: str,
    service_name: str,
) -> dict[str, Any]:
    """Restart a specific service within a deployment.

    Args:
        ctx: SAQ job context
        deployment_id: The deployment ID
        service_name: The service name to restart

    Returns:
        Result dict with status
    """
    logger.info(
        f"Starting restart of service {service_name} in deployment {deployment_id}"
    )

    # Get deployment from database
    async with get_db_context() as db:
        deployment = await db.compose_deployments.get_by_id(deployment_id)

    if not deployment:
        raise ValueError(f"Deployment {deployment_id} not found")

    if not deployment.helm_values or not deployment.helm_values.services:
        raise ValueError(f"Deployment {deployment_id} has no services configured")

    # Check if service exists
    service_names = [service.name for service in deployment.helm_values.services]
    if service_name not in service_names:
        raise ValueError(f"Service '{service_name}' not found in deployment")

    # Find the service by name
    service = next(s for s in deployment.helm_values.services if s.name == service_name)

    # Use WorkloadManager to restart the service
    workload_ops = WorkloadManager(deployment.cluster_id)
    result = await workload_ops.restart_service(
        service=service,
        namespace=deployment.namespace,
    )

    if not result.success:
        raise ValueError(f"Failed to restart service: {result.message}")

    logger.info(f"Successfully restarted service {service_name}")
    return {"status": "success", "service_name": service_name}


async def restart_all_services_job(
    ctx: Context,
    deployment_id: str,
) -> dict[str, Any]:
    """Restart all services within a deployment.

    Args:
        ctx: SAQ job context
        deployment_id: The deployment ID

    Returns:
        Result dict with status
    """
    logger.info(f"Starting restart of all services in deployment {deployment_id}")

    # Get deployment from database
    async with get_db_context() as db:
        deployment = await db.compose_deployments.get_by_id(deployment_id)

    if not deployment:
        raise ValueError(f"Deployment {deployment_id} not found")

    if not deployment.helm_values or not deployment.helm_values.services:
        raise ValueError(f"Deployment {deployment_id} has no services configured")

    # Use WorkloadManager to restart all services
    workload_ops = WorkloadManager(deployment.cluster_id)
    result = await workload_ops.restart_all_services(
        helm_values=deployment.helm_values, namespace=deployment.namespace
    )

    if result.failed > 0:
        raise ValueError(
            f"Failed to restart {result.failed} out of {result.total_services} services"
        )

    logger.info(
        f"Successfully restarted all {result.total_services} services in deployment"
    )
    return {
        "status": "success",
        "total_services": result.total_services,
        "restarted": result.successful,
    }
