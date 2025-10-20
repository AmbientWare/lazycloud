from fastapi import APIRouter, Depends
from sse_starlette.sse import EventSourceResponse

from lazycloud_api.api.dependencies import get_deployment_with_access
from lazycloud_api.api.v1.streaming_utils import create_sse_stream
from lazycloud_api.database.compose import ComposeDeploymentPydantic
from lazycloud_api.services.monitoring import DeploymentMonitor

router = APIRouter()


@router.get("/{deployment_id}/status/stream")
async def stream_deployment_status(
    deployment: ComposeDeploymentPydantic = Depends(get_deployment_with_access),
):
    """Stream real-time deployment status updates."""
    monitor = DeploymentMonitor(
        deployment_id=str(deployment.id),
        namespace=deployment.namespace,
        helm_values=deployment.helm_values,
        callback=None,
    )

    return EventSourceResponse(
        # TODO: we should set up subscriptions to status, logs, and tasks etc.
        create_sse_stream(
            monitor,
            event_type="status",
            format_data=lambda status: {"data": status.model_dump(mode="json")},
            stream_id=f"deployment/{deployment.id}",
        )
    )
