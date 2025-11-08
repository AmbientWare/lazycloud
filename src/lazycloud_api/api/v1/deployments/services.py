from fastapi import APIRouter, Depends, HTTPException
from loguru import logger
from sse_starlette.sse import EventSourceResponse

from lazycloud_api.api.dependencies import (
    get_deployment_with_access,
    get_deployment_with_admin_access,
)
from lazycloud_api.api.utils import (
    create_sse_stream_with_subscription,
)
from lazycloud_api.database.compose import ComposeDeploymentPydantic
from lazycloud_api.prefect_app.services import (
    restart_all_services_task,
    restart_service_task,
)
from lazycloud_api.services.k8s.status_watcher import StatusWatcher
from lazycloud_api.services.monitoring.monitor_config import ServiceMonitorConfig
from shared.models.monitoring import StreamEventType
from shared.models.statuses import TaskStatus
from shared.responses.services import ServiceStatusResponse
from shared.responses.tasks import ServiceTaskStatusResponse

services_router = APIRouter(prefix="/{deployment_id}/services")


@services_router.get(
    "",
    response_model=list[ServiceStatusResponse],
)
async def list_services(
    deployment: ComposeDeploymentPydantic = Depends(get_deployment_with_access),
) -> list[ServiceStatusResponse]:
    """Get the status of all services within a deployment."""
    watcher = StatusWatcher(
        deployment_id=str(deployment.id),
        namespace=deployment.namespace,
        helm_values=deployment.helm_values,
    )

    service_statuses = await watcher.get_service_statuses_for_deployment()

    return [
        ServiceStatusResponse(
            service=service,
            deployment_id=str(deployment.id),
            deployment_name=deployment.name,
            namespace=deployment.namespace,
        )
        for service in service_statuses
    ]


@services_router.get(
    "/{service_name}/status",
    response_model=ServiceStatusResponse,
)
async def get_service_status(
    service_name: str,
    deployment: ComposeDeploymentPydantic = Depends(get_deployment_with_access),
) -> ServiceStatusResponse:
    """Get the status of a specific service within a deployment."""

    watcher = StatusWatcher(
        deployment_id=str(deployment.id),
        namespace=deployment.namespace,
        helm_values=deployment.helm_values,
    )

    service_status = await watcher.get_service_status(service_name)

    if not service_status:
        raise HTTPException(
            status_code=404, detail=f"Service '{service_name}' not found"
        )

    return ServiceStatusResponse(
        deployment_id=str(deployment.id),
        deployment_name=deployment.name,
        namespace=deployment.namespace,
        service=service_status,
    )


@services_router.post(
    "/restart",
    response_model=ServiceTaskStatusResponse,
)
async def restart_all_services(
    deployment: ComposeDeploymentPydantic = Depends(get_deployment_with_admin_access),
) -> ServiceTaskStatusResponse:
    """Restart all services within a deployment."""
    try:
        task_future = restart_all_services_task.delay(
            deployment_id=str(deployment.id),
        )

        return ServiceTaskStatusResponse(
            task_id=task_future.task_run_id,
            status=TaskStatus.PENDING,
            message="Restart all services task submitted",
            deployment_id=str(deployment.id),
        )

    except Exception as e:
        logger.error(f"Failed to submit restart all services task: {e}")
        raise HTTPException(
            status_code=500, detail="Failed to submit restart services task"
        )


@services_router.post(
    "/{service_name}/restart",
    response_model=ServiceTaskStatusResponse,
)
async def restart_service(
    service_name: str,
    deployment: ComposeDeploymentPydantic = Depends(get_deployment_with_admin_access),
) -> ServiceTaskStatusResponse:
    """Restart a specific service within a deployment."""
    try:
        task_future = restart_service_task.delay(
            deployment_id=deployment.id,
            service_name=service_name,
        )

        return ServiceTaskStatusResponse(
            task_id=task_future.task_run_id,
            status=TaskStatus.PENDING,
            message=f"Restart service {service_name} task submitted",
            deployment_id=deployment.id,
        )

    except Exception as e:
        logger.error(f"Failed to submit restart service task: {e}")
        raise HTTPException(
            status_code=500, detail="Failed to submit restart service task"
        )


# Streaming endpoints


@services_router.get("/{service_name}/status/stream")
async def stream_service_status(
    service_name: str,
    deployment: ComposeDeploymentPydantic = Depends(get_deployment_with_access),
):
    """Stream real-time service status updates."""
    config = ServiceMonitorConfig(
        deployment_id=deployment.id,
        service_name=service_name,
        namespace=deployment.namespace,
        helm_values=deployment.helm_values,
        deployment_name=deployment.name,
    )

    return EventSourceResponse(
        create_sse_stream_with_subscription(
            config=config,
            event_type=StreamEventType.STATUS,
            format_data=lambda status: {"data": status.model_dump(mode="json")},
            stream_id=f"service/{deployment.id}/{service_name}",
        )
    )
