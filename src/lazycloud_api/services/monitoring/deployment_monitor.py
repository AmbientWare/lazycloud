from datetime import datetime
from typing import Callable

from lazycloud_api.services.k8s.status_watcher import StatusWatcher
from lazycloud_api.services.monitoring.base import BaseMonitor
from shared.models.helm import (
    HelmValues,
)
from shared.models.statuses import DeploymentStatus


class DeploymentMonitor(BaseMonitor[DeploymentStatus]):
    """Watches a deployment and provides real-time status updates."""

    def __init__(
        self,
        deployment_id: str,
        deployment_name: str,
        namespace: str,
        helm_values: HelmValues,
        deployed_at: datetime,
        callback: Callable[[DeploymentStatus], None],
    ):
        super().__init__("Deployment Monitor", deployment_id, callback)
        self.deployment_id = deployment_id
        self.namespace = namespace
        self.helm_values = helm_values
        self.deployment_name = deployment_name
        self.deployed_at = deployed_at
        self.status_watcher = StatusWatcher(
            deployment_id, namespace, helm_values, deployment_name, deployed_at
        )

    async def _task(self) -> DeploymentStatus:
        return await self.status_watcher.get_deployment_status()
