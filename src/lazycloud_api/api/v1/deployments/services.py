from fastapi import APIRouter, Depends, HTTPException, Query
from loguru import logger
from sse_starlette.sse import EventSourceResponse

from lazycloud_api.api.dependencies import (
    get_deployment_with_access,
    get_deployment_with_admin_access,
)
from lazycloud_api.api.security import get_current_active_user
from lazycloud_api.api.v1.streaming_utils import (
    create_sse_stream_direct,
    create_sse_stream_with_subscription,
)
from lazycloud_api.database.compose import ComposeDeploymentPydantic
from lazycloud_api.database.users import UserPydantic
from lazycloud_api.prefect_app.services import (
    restart_all_services_task,
    restart_service_task,
)
from lazycloud_api.services.k8s.status_watcher import StatusWatcher
from lazycloud_api.services.monitoring import LogMonitor
from lazycloud_api.services.monitoring.monitor_config import ServiceMonitorConfig
from shared.models.monitoring import StreamEventType
from shared.models.statuses import TaskStatus
from shared.responses.services import ServiceStatusResponse
from shared.responses.tasks import ServiceTaskStatusResponse

router = APIRouter()


@router.get(
    "/{deployment_id}/services",
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


@router.get(
    "/{deployment_id}/services/{service_name}/status",
    response_model=ServiceStatusResponse,
)
async def get_service_status(
    deployment: ComposeDeploymentPydantic = Depends(get_deployment_with_access),
    service_name: str = "",
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


@router.post(
    "/{deployment_id}/services/restart",
    response_model=ServiceTaskStatusResponse,
)
async def restart_all_services(
    deployment: ComposeDeploymentPydantic = Depends(get_deployment_with_admin_access),
    current_user: UserPydantic = Depends(get_current_active_user),
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
            service_name=None,
        )

    except Exception as e:
        logger.error(f"Failed to submit restart all services task: {e}")
        raise HTTPException(
            status_code=500, detail="Failed to submit restart services task"
        )


@router.post(
    "/{deployment_id}/services/{service_name}/restart",
    response_model=ServiceTaskStatusResponse,
)
async def restart_service(
    deployment: ComposeDeploymentPydantic = Depends(get_deployment_with_admin_access),
    service_name: str = "",
    current_user: UserPydantic = Depends(get_current_active_user),
) -> ServiceTaskStatusResponse:
    """Restart a specific service within a deployment."""
    try:
        task_future = restart_service_task.delay(
            deployment_id=str(deployment.id),
            service_name=service_name,
        )

        return ServiceTaskStatusResponse(
            task_id=task_future.task_run_id,
            status=TaskStatus.PENDING,
            message=f"Restart service {service_name} task submitted",
            deployment_id=str(deployment.id),
            service_name=service_name,
        )

    except Exception as e:
        logger.error(f"Failed to submit restart service task: {e}")
        raise HTTPException(
            status_code=500, detail="Failed to submit restart service task"
        )


# Streaming endpoints


@router.get("/{deployment_id}/services/{service_name}/status/stream")
async def stream_service_status(
    deployment: ComposeDeploymentPydantic = Depends(get_deployment_with_access),
    service_name: str = "",
):
    """Stream real-time service status updates."""
    config = ServiceMonitorConfig(
        deployment_id=str(deployment.id),
        service_name=service_name,
        namespace=deployment.namespace,
        helm_values=deployment.helm_values,
    )

    return EventSourceResponse(
        create_sse_stream_with_subscription(
            config=config,
            event_type=StreamEventType.STATUS,
            format_data=lambda status: {"data": status.model_dump(mode="json")},
            stream_id=f"service/{deployment.id}/{service_name}",
        )
    )


@router.get("/{deployment_id}/services/{service_name}/pods/{pod_name}/logs/stream")
async def stream_service_logs(
    deployment: ComposeDeploymentPydantic = Depends(get_deployment_with_access),
    service_name: str = "",
    pod_name: str = "",
    tail: int = Query(100),
):
    """Stream real-time service logs."""
    monitor = LogMonitor(
        deployment_id=str(deployment.id),
        namespace=deployment.namespace,
        service_name=service_name,
        pod_name=pod_name,
        tail_lines=tail,
        callback=None,
    )

    return EventSourceResponse(
        create_sse_stream_direct(
            monitor,
            event_type=StreamEventType.LOG,
            format_data=lambda line: {"service": service_name, "line": line},
            stream_id=f"logs/{deployment.id}/{service_name}/{pod_name}",
        )
    )
