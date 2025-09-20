import json
from typing import Any, Callable

from lazycloud_cli.api.base_ws import BaseWsAPI


class StatusAPI(BaseWsAPI):
    def __init__(self):
        super().__init__()

    async def stream_deployment_status(
        self,
        deployment_id: str,
        on_update: Callable[[dict[str, Any]], None],
        on_error: Callable[[Exception], None] | None = None,
        service_name: str | None = None,
    ) -> "StatusAPI":
        """Stream real-time deployment status updates."""
        url_path = f"/deployments/{deployment_id}/status"

        async def on_connect(websocket):
            if service_name:
                msg = {"type": "get_status", "service": service_name}
                await websocket.send(json.dumps(msg))
            else:
                msg = {"type": "get_status"}
                await websocket.send(json.dumps(msg))

        await self.connect_with_retry(
            url_path=url_path,
            on_connect=on_connect,
            on_message=on_update,
            on_error=on_error,
            message_type="status_update",
        )

        return self
