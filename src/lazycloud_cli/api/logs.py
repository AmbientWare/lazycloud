"""
API client for logs operations.
"""

from typing import Any, Callable, Dict, Optional

from lazycloud_cli.api.base_ws import BaseWsAPI


class LogsAPI(BaseWsAPI):
    def __init__(self):
        super().__init__()

    async def stream_logs(
        self,
        deployment_id: str,
        service_name: str,
        tail: int,
        on_message: Callable[[Dict[str, Any]], None],
        on_error: Optional[Callable[[Exception], None]] = None,
    ) -> None:
        """Stream logs from a service.

        Args:
            deployment_id: Deployment ID
            service_name: Service name to get logs from
            tail: Number of lines to show from the end
            on_message: Callback for log messages
            on_error: Optional callback for errors
        """
        url_path = f"/deployments/{deployment_id}/logs/{service_name}?tail={tail}"

        await self.connect_with_retry(
            url_path=url_path,
            on_message=on_message,
            on_error=on_error,
            message_type="log",
        )


logs_api = LogsAPI()
