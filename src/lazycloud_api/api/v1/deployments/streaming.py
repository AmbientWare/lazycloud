from fastapi import APIRouter, Depends, HTTPException
from sse_starlette.sse import EventSourceResponse

from lazycloud_api.api.security import UserData, get_current_active_user
from lazycloud_api.api.v1.streaming_utils import create_sse_stream
from lazycloud_api.database import db
from lazycloud_api.services.monitoring import DeploymentMonitor

router = APIRouter()


@router.get("/{deployment_id}/status/stream")
async def stream_deployment_status(
    deployment_id: str,
    current_user: UserData = Depends(get_current_active_user),
):
    """Stream real-time deployment status updates."""
    deployment = await db.compose_deployments.aget_by_id(deployment_id)
    if not deployment or deployment.user_id != current_user.user_id:
        raise HTTPException(status_code=401, detail="Unauthorized")

    monitor = DeploymentMonitor(
        deployment_id=deployment_id,
        namespace=deployment.namespace,
        helm_values=deployment.helm_values,
        callback=None,
    )

    return EventSourceResponse(
        create_sse_stream(
            monitor,
            event_type="status",
            format_data=lambda status: {"data": status.model_dump(mode="json")},
            stream_id=f"deployment/{deployment_id}",
        )
    )
