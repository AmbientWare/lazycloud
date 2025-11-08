from fastapi import APIRouter, Depends
from sse_starlette.sse import EventSourceResponse

from lazycloud_api.api.dependencies import get_deployment_with_access
from lazycloud_api.api.utils import create_sse_stream_with_subscription
from lazycloud_api.database.compose import ComposeDeploymentPydantic
from lazycloud_api.services.monitoring.monitor_config import DeploymentMonitorConfig
from shared.models.monitoring import StreamEventType

status_router = APIRouter(prefix="/{deployment_id}/status")


@status_router.get("/stream")
async def stream_deployment_status(
    deployment: ComposeDeploymentPydantic = Depends(get_deployment_with_access),
):
    """Stream real-time deployment status updates."""
    config = DeploymentMonitorConfig(
        deployment_id=deployment.id,
        namespace=deployment.namespace,
        helm_values=deployment.helm_values,
        deployment_name=deployment.name,
        deployed_at=deployment.deployed_at,
    )

    return EventSourceResponse(
        create_sse_stream_with_subscription(
            config=config,
            event_type=StreamEventType.STATUS,
            format_data=lambda status: {"data": status.model_dump(mode="json")},
            stream_id=f"deployment/{deployment.id}",
        )
    )
