from fastapi import APIRouter, Depends, HTTPException
from loguru import logger

from lazycloud_api.api.security import UserData, get_current_active_user
from lazycloud_api.database import db
from lazycloud_api.prefect_app.services import (
    restart_all_services_task,
    restart_service_task,
)
from lazycloud_api.services.k8s.status_watcher import StatusWatcher
from shared.models.statuses import TaskStatus
from shared.responses.services import ServiceStatusResponse
from shared.responses.tasks import ServiceTaskStatusResponse

services_router = APIRouter(prefix="/services", tags=["services"])


@services_router.get(
    "/{deployment_id}",
    response_model=list[ServiceStatusResponse],
)
async def get_service_statuses_for_deployment(
    deployment_id: str,
    current_user: UserData = Depends(get_current_active_user),
) -> list[ServiceStatusResponse]:
    """Get the status of all services within a deployment."""
    # Get deployment from database
    deployment = await db.compose_deployments.afind_one(
        {
            "id": deployment_id,
            "user_id": current_user.user_id,
        }
    )
    if not deployment:
        raise HTTPException(status_code=404, detail="Deployment not found")

    # Create watcher and get service status
    watcher = StatusWatcher(
        deployment_id=deployment_id,
        namespace=deployment.namespace,
        helm_values=deployment.helm_values,
    )

    service_statuses = await watcher.get_service_statuses_for_deployment()

    response_data = [
        ServiceStatusResponse(
            service=service,
            deployment_id=deployment_id,
            deployment_name=deployment.name,
            namespace=deployment.namespace,
        )
        for service in service_statuses
    ]

    return response_data


@services_router.get(
    "/{deployment_id}/{service_name}/status",
    response_model=ServiceStatusResponse,
)
async def get_service_status(
    deployment_id: str,
    service_name: str,
    current_user: UserData = Depends(get_current_active_user),
) -> ServiceStatusResponse:
    """Get the status of a specific service within a deployment."""
    # Get deployment from database
    deployment = await db.compose_deployments.afind_one(
        {
            "id": deployment_id,
            "user_id": current_user.user_id,
        }
    )

    if not deployment:
        raise HTTPException(status_code=404, detail="Deployment not found")

    # Create watcher and get service status
    watcher = StatusWatcher(
        deployment_id=deployment_id,
        namespace=deployment.namespace,
        helm_values=deployment.helm_values,
    )

    service_status = await watcher.get_service_status(service_name)

    if not service_status:
        raise HTTPException(
            status_code=404, detail=f"Service '{service_name}' not found"
        )

    return ServiceStatusResponse(
        deployment_id=deployment_id,
        deployment_name=deployment.name,
        namespace=deployment.namespace,
        service=service_status,
    )


@services_router.post(
    "/restart/{deployment_id}", response_model=ServiceTaskStatusResponse
)
async def restart_all_services(
    deployment_id: str,
    current_user: UserData = Depends(get_current_active_user),
) -> ServiceTaskStatusResponse:
    """Restart all services within a deployment."""
    try:
        # Submit task to restart all services
        task_future = restart_all_services_task.delay(
            deployment_id=deployment_id,
            user_id=current_user.user_id,
        )

        return ServiceTaskStatusResponse(
            task_id=task_future.task_run_id,
            status=TaskStatus.PENDING,
            message="Restart all services task submitted",
            deployment_id=deployment_id,
            service_name=None,  # None indicates all services
        )

    except Exception as e:
        logger.error(f"Failed to submit restart all services task: {e}")
        raise HTTPException(
            status_code=500, detail="Failed to submit restart services task"
        )


@services_router.post(
    "/restart/{deployment_id}/{service_name}",
    response_model=ServiceTaskStatusResponse,
)
async def restart_service(
    deployment_id: str,
    service_name: str,
    current_user: UserData = Depends(get_current_active_user),
) -> ServiceTaskStatusResponse:
    """Restart a specific service within a deployment."""
    try:
        # Submit task to restart the service
        task_future = restart_service_task.delay(
            deployment_id=deployment_id,
            service_name=service_name,
            user_id=current_user.user_id,
        )

        return ServiceTaskStatusResponse(
            task_id=task_future.task_run_id,
            status=TaskStatus.PENDING,
            message=f"Restart service {service_name} task submitted",
            deployment_id=deployment_id,
            service_name=service_name,
        )

    except Exception as e:
        logger.error(f"Failed to submit restart service task: {e}")
        raise HTTPException(
            status_code=500, detail="Failed to submit restart service task"
        )
