"""Service management flows - restart services."""

from loguru import logger
from prefect import flow

from backend.database import get_db_context
from backend.services.k8s.workload_manager import WorkloadManager


@flow(name="restart-service-flow", log_prints=True)
async def restart_service_flow(deployment_id: str, service_name: str) -> None:
    """Restart a specific service within a deployment."""
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

    # Use WorkloadOperations to restart the service
    workload_ops = WorkloadManager()
    result = workload_ops.restart_service(
        service=service,
        namespace=deployment.namespace,
    )

    if not result.success:
        raise ValueError(f"Failed to restart service: {result.message}")

    logger.info(f"Successfully restarted service {service_name}")


@flow(name="restart-all-services-flow", log_prints=True)
async def restart_all_services_flow(deployment_id: str) -> None:
    """Restart all services within a deployment."""
    logger.info(f"Starting restart of all services in deployment {deployment_id}")

    # Get deployment from database
    async with get_db_context() as db:
        deployment = await db.compose_deployments.get_by_id(deployment_id)

    if not deployment:
        raise ValueError(f"Deployment {deployment_id} not found")

    if not deployment.helm_values or not deployment.helm_values.services:
        raise ValueError(f"Deployment {deployment_id} has no services configured")

    # Use WorkloadOperations to restart all services
    workload_ops = WorkloadManager()
    result = workload_ops.restart_all_services(
        helm_values=deployment.helm_values, namespace=deployment.namespace
    )

    if result.failed > 0:
        raise ValueError(
            f"Failed to restart {result.failed} out of {result.total_services} services"
        )

    logger.info(
        f"Successfully restarted all {result.total_services} services in deployment"
    )
