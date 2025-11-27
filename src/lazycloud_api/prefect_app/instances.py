from loguru import logger
from prefect import task

from lazycloud_api.database import get_db_context
from lazycloud_api.services.k8s.pod_manager import KubernetesPodManager


@task(log_prints=True)
async def delete_instance_task(
    deployment_id: str, service_name: str, pod_name: str, force: bool = False
) -> None:
    """Delete a specific instance (pod) in a deployment."""
    logger.info(
        f"Starting deletion of instance {pod_name} in deployment {deployment_id} for service {service_name} (force={force})"
    )

    # Get the deployment
    async with get_db_context() as db:
        deployment = await db.compose_deployments.get_by_id(deployment_id)
        if not deployment:
            raise Exception(f"Deployment {deployment_id} not found")

    # Validate service exists in deployment
    helm_values = deployment.helm_values
    if not helm_values or not helm_values.services:
        raise Exception("Deployment does not have service configuration")

    service = next((s for s in helm_values.services if s.name == service_name), None)
    if not service:
        raise Exception(f"Service '{service_name}' not found in deployment")

    namespace = deployment.namespace
    resource_name = service.resourceName

    # Initialize pod manager
    pod_manager = KubernetesPodManager()

    # Verify pod ownership before deletion
    logger.info(f"Verifying pod {pod_name} belongs to service {service_name}")
    verification_result = pod_manager.verify_pod_ownership(
        pod_name=pod_name,
        namespace=namespace,
        service_name=service_name,
        resource_name=resource_name,
    )

    if not verification_result.success:
        # If pod doesn't exist, that's fine - deletion goal already achieved
        if "does not exist" in (verification_result.error or "").lower():
            logger.info(
                f"Pod {pod_name} does not exist - deletion goal already achieved"
            )
            return

        else:
            # Other verification errors (e.g., wrong ownership) are real errors
            raise Exception(
                verification_result.error or "Pod ownership verification failed"
            )

    # Delete the pod
    logger.info(f"Deleting pod {pod_name} in namespace {namespace} (force={force})")
    delete_result = pod_manager.delete_pod(
        pod_name=pod_name,
        namespace=namespace,
        grace_period=0 if force else 30,  # No grace period if forcing
        force=force,
    )

    if not delete_result.success:
        raise Exception(delete_result.error or "Failed to delete pod")

    logger.info(f"Successfully deleted instance {pod_name}")
