"""
API client for real-time status operations.
"""

import json
from typing import Any, Callable, Dict, Optional

from lazycloud_cli.api.base_ws import BaseWsAPI


class StatusAPI(BaseWsAPI):
    def __init__(self):
        super().__init__()

    async def stream_deployment_status(
        self,
        deployment_id: str,
        on_update: Callable[[Dict[str, Any]], None],
        on_error: Optional[Callable[[Exception], None]] = None,
        service_name: Optional[str] = None,
    ) -> "StatusAPI":
        """Stream real-time deployment status updates."""
        url_path = f"/deployments/{deployment_id}/status"

        async def on_connect(websocket):
            if service_name:
                msg = {"type": "get_status", "service": service_name}
                print(f"DEBUG: Sending service-specific status request: {msg}")
                await websocket.send(json.dumps(msg))
            else:
                msg = {"type": "get_status"}
                print(f"DEBUG: Sending general status request: {msg}")
                await websocket.send(json.dumps(msg))

        await self.connect_with_retry(
            url_path=url_path,
            on_connect=on_connect,
            on_message=on_update,
            on_error=on_error,
            message_type="status_update",
        )

        return self


status_api = StatusAPI()
