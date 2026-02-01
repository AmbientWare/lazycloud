from datetime import datetime
from typing import Callable

from models.billing import UsageUnits
from models.helm import HelmValues
from models.statuses import DeploymentStatus

from backend.services.k8s.status_watcher import StatusWatcher
from backend.services.monitoring.base import BaseMonitor
from backend.services.storage_sizes import get_storage_sizes_cached


class DeploymentMonitor(BaseMonitor[DeploymentStatus]):
    """Watches a deployment and provides real-time status updates."""

    def __init__(
        self,
        deployment_id: str,
        deployment_name: str,
        namespace: str,
        helm_values: HelmValues,
        deployed_at: datetime,
        cluster_id: str,
        callback: Callable[[DeploymentStatus], None] | None = None,
    ):
        super().__init__("Deployment Monitor", deployment_id, callback)
        self.deployment_id = deployment_id
        self.namespace = namespace
        self.helm_values = helm_values
        self.deployment_name = deployment_name
        self.deployed_at = deployed_at
        self.status_watcher = StatusWatcher(
            deployment_id=deployment_id,
            namespace=namespace,
            helm_values=helm_values,
            cluster_id=cluster_id,
            deployment_name=deployment_name,
            deployed_at=deployed_at,
        )

    async def _task(self) -> DeploymentStatus:
        status = await self.status_watcher.get_deployment_status()

        # Enrich volume summaries with storage sizes from billing data (cached)
        if status.volumes:
            storage_sizes = await get_storage_sizes_cached(self.deployment_id)

            for volume in status.volumes:
                if volume.name in storage_sizes:
                    _, size_gb = storage_sizes[volume.name]
                    volume.size = UsageUnits.format_size_gb(size_gb)

        return status
