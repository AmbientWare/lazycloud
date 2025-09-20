from fastapi import APIRouter, Depends, HTTPException
from loguru import logger

from lazycloud_api.api.security import UserData, get_current_active_user
from lazycloud_api.database import db
from lazycloud_api.services.k8s.workload_operations import WorkloadOperations
from shared.responses.deployments import RestartResponse, RestartServiceResult

services_router = APIRouter(prefix="/services", tags=["services"])


@services_router.post("/restart/{deployment_id}", response_model=RestartResponse)
async def restart_all_services(
    deployment_id: str,
    current_user: UserData = Depends(get_current_active_user),
) -> RestartResponse:
    """Restart all services within a deployment."""
    try:
        # Get deployment from database
        deployment = await db.compose_deployments.afind_one(
            {
                "id": deployment_id,
                "user_id": current_user.user_id,
            }
        )

        if not deployment:
            raise HTTPException(status_code=404, detail="Deployment not found")

        # Use WorkloadOperations to restart all services
        workload_ops = WorkloadOperations()
        result = workload_ops.restart_all_services(
            helm_values=deployment.helm_values, namespace=deployment.namespace
        )

        # Convert RestartResult objects to RestartServiceResult
        service_results: list[RestartServiceResult] = []
        service_names = [service.name for service in deployment.helm_values.services]
        for service_name, restart_result in zip(service_names, result.results):
            service_results.append(
                RestartServiceResult(
                    service=service_name,
                    success=restart_result.success,
                    message=restart_result.message,
                    resource_type=restart_result.resource_type,
                    resource_name=restart_result.resource_name,
                    output=restart_result.output,
                    error=restart_result.error,
                )
            )

        success = result.failed == 0
        if result.failed > 0:
            message = f"Restart completed with {result.failed} failures"
        else:
            message = f"Successfully restarted all {result.total_services} services"

        return RestartResponse(
            deployment_id=deployment_id,
            deployment_name=deployment.name,
            service_name=None,  # None indicates all services
            success=success,
            message=message,
            total_services=result.total_services,
            successful=result.successful,
            failed=result.failed,
            results=service_results,
        )

    except HTTPException:
        raise

    except Exception as e:
        logger.error(f"Failed to restart all services: {e}")
        raise HTTPException(status_code=500, detail="Failed to restart services")


@services_router.post(
    "/restart/{deployment_id}/{service_name}",
    response_model=RestartResponse,
)
async def restart_service(
    deployment_id: str,
    service_name: str,
    current_user: UserData = Depends(get_current_active_user),
) -> RestartResponse:
    """Restart a specific service within a deployment."""
    try:
        # Get deployment from database
        deployment = await db.compose_deployments.afind_one(
            {
                "id": deployment_id,
                "user_id": current_user.user_id,
            }
        )

        if not deployment:
            raise HTTPException(status_code=404, detail="Deployment not found")

        # Check if service exists
        service_names = [service.name for service in deployment.helm_values.services]
        if service_name not in service_names:
            raise HTTPException(
                status_code=404,
                detail=f"Service '{service_name}' not found in deployment",
            )

        # Find the service by name
        service = next(
            s for s in deployment.helm_values.services if s.name == service_name
        )

        # Use WorkloadOperations to restart the service
        workload_ops = WorkloadOperations()
        result = workload_ops.restart_service(
            service=service,
            namespace=deployment.namespace,
        )

        return RestartResponse(
            deployment_id=deployment_id,
            deployment_name=deployment.name,
            service_name=service_name,
            success=result.success,
            message=result.message,
            resource_type=result.resource_type,
            resource_name=result.resource_name,
            output=result.output,
        )

    except HTTPException:
        raise

    except Exception as e:
        logger.error(f"Failed to restart service: {e}")
        raise HTTPException(status_code=500, detail="Failed to restart service")
