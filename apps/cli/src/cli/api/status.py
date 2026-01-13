from datetime import UTC
from typing import Callable

from loguru import logger
from models.statuses import DeploymentStatus, ServiceStatus

from cli.api.base_sse import SSEClient


class StatusAPI:
    """API for streaming real-time status updates.

    Note: Each stream method creates its own SSEClient instance to avoid
    shared state issues when multiple streams run concurrently.
    """

    async def stream_deployment_status(
        self,
        deployment_id: str,
        on_update: Callable[[DeploymentStatus], None],
        on_error: Callable[[Exception], None] | None = None,
    ) -> None:
        """Stream deployment status updates."""
        client = SSEClient()

        def handle_event(event_type: str, data: dict) -> None:
            if event_type == "status":
                status_data = data.get("data", {})
                status = DeploymentStatus(**status_data)
                # Convert UTC times to local timezone
                status.last_checked = status.last_checked.replace(
                    tzinfo=UTC
                ).astimezone()
                status.deployed_at = status.deployed_at.replace(tzinfo=UTC).astimezone()
                on_update(status)

        await client.stream(
            path=f"/deployments/{deployment_id}/status/stream",
            on_event=handle_event,
            on_error=on_error,
        )

    async def stream_service_status(
        self,
        deployment_id: str,
        service_name: str,
        on_update: Callable[[ServiceStatus], None],
        on_error: Callable[[Exception], None] | None = None,
    ) -> None:
        """Stream service status updates."""
        logger.debug(f"StatusAPI.stream_service_status called for {service_name}")
        client = SSEClient()

        def handle_event(event_type: str, data: dict) -> None:
            logger.debug(f"StatusAPI.stream_service_status received event: {event_type}")
            if event_type == "status":
                status_data = data.get("data", {})
                status = ServiceStatus(**status_data)
                # Convert UTC time to local timezone
                status.last_checked = status.last_checked.replace(
                    tzinfo=UTC
                ).astimezone()
                on_update(status)

        await client.stream(
            path=f"/deployments/{deployment_id}/services/{service_name}/status/stream",
            on_event=handle_event,
            on_error=on_error,
        )
