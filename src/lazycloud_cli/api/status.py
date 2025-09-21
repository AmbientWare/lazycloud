import json
from datetime import UTC
from typing import Callable

from lazycloud_cli.api.base_ws import BaseWsAPI
from shared.models.statuses import DeploymentStatus, ServiceStatus


class StatusAPI(BaseWsAPI):
    def __init__(self):
        super().__init__()

    async def stream_deployment_status(
        self,
        deployment_id: str,
        on_update: Callable[[DeploymentStatus], None],
        on_error: Callable[[Exception], None] | None = None,
    ) -> "StatusAPI":
        """Stream real-time deployment status updates."""
        url_path = f"/deployments/{deployment_id}/status"

        async def on_connect(websocket):
            msg = {"type": "get_status"}
            await websocket.send(json.dumps(msg))

        def _on_message(data: dict) -> None:
            status = DeploymentStatus(**data)
            # NOTE: time from api is in UTC, convert to local
            status.last_checked = status.last_checked.replace(tzinfo=UTC).astimezone()
            on_update(status)

        await self.connect_with_retry(
            url_path=url_path,
            on_connect=on_connect,
            on_message=_on_message,
            on_error=on_error,
            message_type="status",
        )

        return self

    async def stream_service_status(
        self,
        deployment_id: str,
        service_name: str,
        on_update: Callable[[ServiceStatus], None],
        on_error: Callable[[Exception], None] | None = None,
    ) -> "StatusAPI":
        """Stream real-time service status updates."""
        url_path = f"/services/{deployment_id}/{service_name}/status"

        async def on_connect(websocket):
            msg = {"type": "get_status"}
            await websocket.send(json.dumps(msg))

        def _on_message(data: dict) -> None:
            status = ServiceStatus(**data)
            # NOTE: time from api is in UTC, convert to local
            status.last_checked = status.last_checked.replace(tzinfo=UTC).astimezone()
            on_update(status)

        await self.connect_with_retry(
            url_path=url_path,
            on_connect=on_connect,
            on_message=_on_message,
            on_error=on_error,
            message_type="status",
        )

        return self
