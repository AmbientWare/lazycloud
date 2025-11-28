from fastapi import APIRouter, Depends, HTTPException, Query
from loguru import logger
from models.deployments import DeploymentStates
from models.helm import ServiceValues
from models.monitoring import StreamEventType
from models.statuses import TaskStatus
from responses.tasks import InstanceTaskStatusResponse
from sse_starlette.sse import EventSourceResponse

from backend.api.dependencies import (
    get_deployment_with_access,
    get_deployment_with_admin_access,
)
from backend.api.utils import create_sse_stream_with_subscription
from backend.database.compose import ComposeDeploymentPydantic
from backend.prefect_app.instances import delete_instance_task
from backend.services.monitoring.monitor_config import LogMonitorConfig

instances_router = APIRouter(prefix="/{deployment_id}/instances")


def _find_service_for_pod(
    pod_name: str, services: list[ServiceValues]
) -> ServiceValues | None:
    """Find which service a pod belongs to based on naming convention"""
    # Sort by longest name first to avoid substring matches
    services_sorted = sorted(services, key=lambda s: len(s.name), reverse=True)

    return next(
        (
            s
            for s in services_sorted
            if pod_name.startswith(f"{s.name}-") or pod_name == s.name
        ),
        None,
    )


@instances_router.delete("/{pod_name}")
async def delete_instance(
    pod_name: str,
    force: bool = Query(
        False, description="Force delete the pod (bypasses graceful shutdown)"
    ),
    deployment: ComposeDeploymentPydantic = Depends(get_deployment_with_admin_access),
) -> InstanceTaskStatusResponse:
    """Delete a specific instance in a deployment."""
    try:
        # Check deployment state - don't allow instance deletion if deployment is being deleted
        if deployment.state in [DeploymentStates.DELETING]:
            raise HTTPException(
                status_code=400,
                detail="Cannot delete instance: deployment is being deleted",
            )

        # Basic validation that service exists
        helm_values = deployment.helm_values
        if not helm_values or not helm_values.services:
            raise HTTPException(
                status_code=400,
                detail="Deployment does not have service configuration",
            )

        # Find the service this pod belongs to
        service = _find_service_for_pod(pod_name, helm_values.services)
        if not service:
            raise HTTPException(
                status_code=404,
                detail=f"Pod '{pod_name}' does not belong to any service in this deployment",
            )

        # Submit task to delete the instance
        logger.info(
            f"Submitting delete instance task for pod {pod_name} in deployment {deployment.id} (force={force})"
        )

        task_future = delete_instance_task.delay(
            deployment_id=deployment.id,
            service_name=service.name,
            pod_name=pod_name,
            force=force,
        )

        return InstanceTaskStatusResponse(
            task_id=task_future.task_run_id,
            status=TaskStatus.PENDING,
            message=f"Instance {pod_name} deletion task submitted",
            deployment_id=deployment.id,
            service_name=service.name,
            pod_name=pod_name,
        )

    except HTTPException:
        raise

    except Exception as e:
        logger.error(f"Failed to submit delete instance task: {e}")
        raise HTTPException(
            status_code=500, detail="Failed to submit instance deletion task"
        )


@instances_router.get("/{pod_name}/logs/stream")
async def stream_service_logs(
    pod_name: str,
    tail: int = Query(100),
    deployment: ComposeDeploymentPydantic = Depends(get_deployment_with_access),
):
    """Stream real-time service logs."""
    # Find which service this pod belongs to
    service = _find_service_for_pod(pod_name, deployment.helm_values.services)

    if not service:
        raise HTTPException(
            status_code=404,
            detail=f"Pod '{pod_name}' does not belong to any service in this deployment",
        )

    service_name = service.name

    config = LogMonitorConfig(
        deployment_id=deployment.id,
        namespace=deployment.namespace,
        service_name=service_name,
        pod_name=pod_name,
        tail_lines=tail,
    )

    return EventSourceResponse(
        create_sse_stream_with_subscription(
            config=config,
            event_type=StreamEventType.LOG,
            format_data=lambda line: {"service": service_name, "line": line},
            stream_id=f"logs/{deployment.id}/{service_name}/{pod_name}",
        )
    )
