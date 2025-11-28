from typing import Any, Callable

from cli.api.base_sse import SSEClient


class LogsAPI:
    """API for streaming real-time service logs."""

    def __init__(self):
        self._client = SSEClient()

    async def stream_logs(
        self,
        deployment_id: str,
        pod_name: str,
        tail: int,
        on_message: Callable[[dict[str, Any]], None],
        on_error: Callable[[Exception], None] | None = None,
    ) -> None:
        """Stream logs from a service pod"""

        def handle_event(event_type: str, data: dict) -> None:
            if event_type == "log":
                on_message(data)

        await self._client.stream(
            path=f"/deployments/{deployment_id}/instances/{pod_name}/logs/stream?tail={tail}",
            on_event=handle_event,
            on_error=on_error,
        )

    async def disconnect(self):
        """Disconnect active streams."""
        await self._client.disconnect()

    def is_connected(self) -> bool:
        """Check if connected to a stream."""
        return self._client.is_connected()
