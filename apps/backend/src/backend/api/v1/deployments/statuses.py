from fastapi import APIRouter, Depends
from models.monitoring import StreamEventType
from sse_starlette.sse import EventSourceResponse

from backend.api.dependencies import get_deployment_with_access
from backend.api.utils import create_sse_stream_with_subscription
from backend.database.models import ComposeDeploymentInDb
from backend.services.monitoring.monitor_config import (
    DeploymentMonitorConfig,
    DeployProgressMonitorConfig,
)

status_router = APIRouter(prefix="/{deployment_id}/status")


@status_router.get("/stream")
async def stream_deployment_status(
    deployment: ComposeDeploymentInDb = Depends(get_deployment_with_access),
):
    """Stream real-time deployment status updates."""
    config = DeploymentMonitorConfig(
        deployment_id=deployment.id,
        namespace=deployment.namespace,
        helm_values=deployment.helm_values,
        deployment_name=deployment.name,
        deployed_at=deployment.deployed_at,
        cluster_id=deployment.cluster_id,
    )

    return EventSourceResponse(
        create_sse_stream_with_subscription(
            config=config,
            event_type=StreamEventType.STATUS,
            format_data=lambda status: {"data": status.model_dump(mode="json")},
            stream_id=f"deployment/{deployment.id}",
        )
    )


@status_router.get("/deploy/stream")
async def stream_deploy_progress(
    deployment: ComposeDeploymentInDb = Depends(get_deployment_with_access),
):
    """Stream deployment progress with per-service status and early failure detection."""
    config = DeployProgressMonitorConfig(
        deployment_id=str(deployment.id),
        deployment_name=deployment.name,
        namespace=deployment.namespace,
        helm_values=deployment.helm_values,
        cluster_id=deployment.cluster_id,
    )

    return EventSourceResponse(
        create_sse_stream_with_subscription(
            config=config,
            event_type=StreamEventType.DEPLOY_PROGRESS,
            format_data=lambda progress: progress.model_dump(mode="json"),
            stream_id=f"deploy_progress/{deployment.id}",
        )
    )
