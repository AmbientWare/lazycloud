from typing import Callable

from lazycloud_api.services.k8s.log_streamer import LogStreamer
from lazycloud_api.services.monitoring.base import BaseGenerativeMonitor


class LogMonitor(BaseGenerativeMonitor[str]):
    """Monitors and streams logs from Kubernetes pods."""

    def __init__(
        self,
        deployment_id: str,
        namespace: str,
        service_name: str,
        pod_name: str | None = None,
        tail_lines: int = 100,
        callback: Callable[[str], None] | None = None,
    ):
        detail = f"{pod_name or service_name} in {namespace}"
        super().__init__("Log Monitor", detail, callback)

        self.log_streamer = LogStreamer(
            deployment_id=deployment_id,
            namespace=namespace,
            service_name=service_name,
            follow=True,
            tail_lines=tail_lines,
            pod_name=pod_name,
        )

    async def _stream(self):
        async for log_line in self.log_streamer.stream():
            if not self._running:
                break
            await self._emit(log_line)

    async def _cleanup(self):
        await self.log_streamer.stop()
