from fastapi import APIRouter, Depends, HTTPException, Query
from loguru import logger
from sse_starlette.sse import EventSourceResponse

from lazycloud_api.api.security import get_current_active_user
from lazycloud_api.api.v1.streaming_utils import create_sse_stream
from lazycloud_api.database import db
from lazycloud_api.database.users import UserPydantic
from lazycloud_api.prefect_app.services import (
    restart_all_services_task,
    restart_service_task,
)
from lazycloud_api.services.k8s.status_watcher import StatusWatcher
from lazycloud_api.services.monitoring import LogMonitor, ServiceMonitor
from shared.models.statuses import TaskStatus
from shared.responses.services import ServiceStatusResponse
from shared.responses.tasks import ServiceTaskStatusResponse

router = APIRouter()


@router.get(
    "/{deployment_id}/services",
    response_model=list[ServiceStatusResponse],
)
async def list_services(
    deployment_id: str,
    current_user: UserPydantic = Depends(get_current_active_user),
) -> list[ServiceStatusResponse]:
    """Get the status of all services within a deployment."""
    deployment = await db.compose_deployments.afind_one(
        {
            "id": deployment_id,
            "user_id": current_user.id,
        }
    )
    if not deployment:
        raise HTTPException(status_code=404, detail="Deployment not found")

    watcher = StatusWatcher(
        deployment_id=deployment_id,
        namespace=deployment.namespace,
        helm_values=deployment.helm_values,
    )

    service_statuses = await watcher.get_service_statuses_for_deployment()

    return [
        ServiceStatusResponse(
            service=service,
            deployment_id=deployment_id,
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
    deployment_id: str,
    service_name: str,
    current_user: UserPydantic = Depends(get_current_active_user),
) -> ServiceStatusResponse:
    """Get the status of a specific service within a deployment."""
    deployment = await db.compose_deployments.afind_one(
        {
            "id": deployment_id,
            "user_id": current_user.id,
        }
    )

    if not deployment:
        raise HTTPException(status_code=404, detail="Deployment not found")

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


@router.post(
    "/{deployment_id}/services/restart",
    response_model=ServiceTaskStatusResponse,
)
async def restart_all_services(
    deployment_id: str,
    current_user: UserPydantic = Depends(get_current_active_user),
) -> ServiceTaskStatusResponse:
    """Restart all services within a deployment."""
    try:
        task_future = restart_all_services_task.delay(
            deployment_id=deployment_id,
            user_id=current_user.id,
        )

        return ServiceTaskStatusResponse(
            task_id=task_future.task_run_id,
            status=TaskStatus.PENDING,
            message="Restart all services task submitted",
            deployment_id=deployment_id,
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
    deployment_id: str,
    service_name: str,
    current_user: UserPydantic = Depends(get_current_active_user),
) -> ServiceTaskStatusResponse:
    """Restart a specific service within a deployment."""
    try:
        task_future = restart_service_task.delay(
            deployment_id=deployment_id,
            service_name=service_name,
            user_id=current_user.id,
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


# Streaming endpoints


@router.get("/{deployment_id}/services/{service_name}/status/stream")
async def stream_service_status(
    deployment_id: str,
    service_name: str,
    current_user: UserPydantic = Depends(get_current_active_user),
):
    """Stream real-time service status updates."""
    deployment = await db.compose_deployments.aget_by_id(deployment_id)
    if not deployment or deployment.user_id != current_user.id:
        raise HTTPException(status_code=401, detail="Unauthorized")

    monitor = ServiceMonitor(
        deployment_id=deployment_id,
        service_name=service_name,
        namespace=deployment.namespace,
        helm_values=deployment.helm_values,
        callback=None,
    )

    return EventSourceResponse(
        create_sse_stream(
            monitor,
            event_type="status",
            format_data=lambda status: {"data": status.model_dump(mode="json")},
            stream_id=f"service/{deployment_id}/{service_name}",
        )
    )


@router.get("/{deployment_id}/services/{service_name}/pods/{pod_name}/logs/stream")
async def stream_service_logs(
    deployment_id: str,
    service_name: str,
    pod_name: str,
    current_user: UserPydantic = Depends(get_current_active_user),
    tail: int = Query(100),
):
    """Stream real-time service logs."""
    deployment = await db.compose_deployments.aget_by_id(deployment_id)
    if not deployment or deployment.user_id != current_user.user_id:
        raise HTTPException(status_code=401, detail="Unauthorized")

    monitor = LogMonitor(
        deployment_id=deployment_id,
        namespace=deployment.namespace,
        service_name=service_name,
        pod_name=pod_name,
        tail_lines=tail,
        callback=None,
    )

    return EventSourceResponse(
        create_sse_stream(
            monitor,
            event_type="log",
            format_data=lambda line: {"service": service_name, "line": line},
            stream_id=f"logs/{deployment_id}/{service_name}/{pod_name}",
        )
    )
