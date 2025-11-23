from loguru import logger
from prefect import task

from lazycloud_api.database import db
from lazycloud_api.services.k8s.workload_manager import WorkloadManager


@task
async def restart_service_task(deployment_id: str, service_name: str) -> None:
    """Restart a specific service within a deployment."""
    logger.info(
        f"Starting restart of service {service_name} in deployment {deployment_id}"
    )

    # Get deployment from database
    deployment = await db.compose_deployments.get_by_id(deployment_id)

    if not deployment:
        raise Exception(f"Deployment {deployment_id} not found")

    # Check if service exists
    service_names = [service.name for service in deployment.helm_values.services]
    if service_name not in service_names:
        raise Exception(f"Service '{service_name}' not found in deployment")

    # Find the service by name
    service = next(s for s in deployment.helm_values.services if s.name == service_name)

    # Use WorkloadOperations to restart the service
    workload_ops = WorkloadManager()
    result = workload_ops.restart_service(
        service=service,
        namespace=deployment.namespace,
    )

    if not result.success:
        raise Exception(f"Failed to restart service: {result.message}")

    logger.info(f"Successfully restarted service {service_name}")


@task
async def restart_all_services_task(deployment_id: str) -> None:
    """Restart all services within a deployment."""
    logger.info(f"Starting restart of all services in deployment {deployment_id}")

    # Get deployment from database
    deployment = await db.compose_deployments.get_by_id(deployment_id)

    if not deployment:
        raise Exception(f"Deployment {deployment_id} not found")

    # Use WorkloadOperations to restart all services
    workload_ops = WorkloadManager()
    result = workload_ops.restart_all_services(
        helm_values=deployment.helm_values, namespace=deployment.namespace
    )

    if result.failed > 0:
        raise Exception(
            f"Failed to restart {result.failed} out of {result.total_services} services"
        )

    logger.info(
        f"Successfully restarted all {result.total_services} services in deployment"
    )
