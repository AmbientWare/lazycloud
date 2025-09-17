"""
API client for logs operations.
"""

from typing import Any, Callable

from lazycloud_cli.api.base_ws import BaseWsAPI


class LogsAPI(BaseWsAPI):
    def __init__(self):
        super().__init__()

    async def stream_logs(
        self,
        deployment_id: str,
        service_name: str,
        tail: int,
        on_message: Callable[[dict[str, Any]], None],
        on_error: Callable[[Exception], None] | None = None,
        pod_name: str | None = None,
    ) -> None:
        """Stream logs from a service"""
        url_path = f"/deployments/{deployment_id}/logs/{service_name}?tail={tail}"

        if pod_name:
            url_path += f"&pod={pod_name}"

        await self.connect_with_retry(
            url_path=url_path,
            on_message=on_message,
            on_error=on_error,
            message_type="log",
        )


logs_api = LogsAPI()
