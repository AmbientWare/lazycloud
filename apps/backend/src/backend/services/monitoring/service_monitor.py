from typing import Callable

from models.helm import (
    HelmValues,
)
from models.statuses import ServiceStatus

from backend.services.k8s.status_watcher import StatusWatcher
from backend.services.monitoring.base import BaseMonitor


class ServiceMonitor(BaseMonitor[ServiceStatus]):
    """Watches a specific service and provides real-time status updates."""

    def __init__(
        self,
        deployment_id: str,
        deployment_name: str,
        service_name: str,
        namespace: str,
        helm_values: HelmValues,
        cluster_id: str,
        callback: Callable[[ServiceStatus], None] | None = None,
    ):
        super().__init__("Service Monitor", service_name, callback)
        self.deployment_id = deployment_id
        self.service_name = service_name
        self.namespace = namespace
        self.helm_values = helm_values
        self.deployment_name = deployment_name
        self.status_watcher = StatusWatcher(
            deployment_id=deployment_id,
            namespace=namespace,
            helm_values=helm_values,
            cluster_id=cluster_id,
            deployment_name=deployment_name,
            deployed_at=None,
        )

    async def _task(self) -> ServiceStatus | None:
        return await self.status_watcher.get_service_status(self.service_name)
