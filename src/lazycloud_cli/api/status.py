from datetime import UTC
from typing import Callable

from lazycloud_cli.api.base_sse import SSEClient
from shared.models.statuses import DeploymentStatus, ServiceStatus


class StatusAPI:
    """API for streaming real-time status updates."""

    def __init__(self):
        self._client = SSEClient()

    async def stream_deployment_status(
        self,
        deployment_id: str,
        on_update: Callable[[DeploymentStatus], None],
        on_error: Callable[[Exception], None] | None = None,
    ) -> None:
        """Stream deployment status updates."""

        def handle_event(event_type: str, data: dict) -> None:
            if event_type == "status":
                status_data = data.get("data", {})
                status = DeploymentStatus(**status_data)
                # Convert UTC time to local timezone
                status.last_checked = status.last_checked.replace(
                    tzinfo=UTC
                ).astimezone()
                on_update(status)

        await self._client.stream(
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

        def handle_event(event_type: str, data: dict) -> None:
            if event_type == "status":
                status_data = data.get("data", {})
                status = ServiceStatus(**status_data)
                # Convert UTC time to local timezone
                status.last_checked = status.last_checked.replace(
                    tzinfo=UTC
                ).astimezone()
                on_update(status)

        await self._client.stream(
            path=f"/deployments/{deployment_id}/services/{service_name}/status/stream",
            on_event=handle_event,
            on_error=on_error,
        )

    async def disconnect(self):
        """Disconnect active streams."""
        await self._client.disconnect()

    def is_connected(self) -> bool:
        """Check if connected to a stream."""
        return self._client.is_connected()
