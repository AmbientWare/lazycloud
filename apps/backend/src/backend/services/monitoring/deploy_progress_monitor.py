"""Deploy progress monitor - shows container states during deploys."""

import time
from typing import Callable

from models.helm import HelmValues
from models.k8s import WorkloadType
from models.monitoring import (
    DeployProgressStatus,
    DeployServiceStatus,
)
from models.pod_states import ContainerCounts
from models.statuses import ServiceStatus, StatusPhase

from backend.services.k8s.status_watcher import StatusWatcher
from backend.services.monitoring.base import BaseMonitor


class DeployProgressMonitor(BaseMonitor[DeployProgressStatus]):
    """Monitors deployment progress with Docker-like container counts."""

    def __init__(
        self,
        deployment_id: str,
        deployment_name: str,
        namespace: str,
        helm_values: HelmValues,
        cluster_id: str,
        callback: Callable[[DeployProgressStatus], None] | None = None,
    ):
        super().__init__(
            "Deploy Progress Monitor",
            deployment_id,
            callback,
            always_emit=True,
        )
        self.deployment_id = deployment_id
        self.deployment_name = deployment_name
        self.namespace = namespace
        self.helm_values = helm_values
        self.start_time = time.time()

        self.status_watcher = StatusWatcher(
            deployment_id=deployment_id,
            namespace=namespace,
            helm_values=helm_values,
            cluster_id=cluster_id,
            deployment_name=deployment_name,
            deployed_at=None,
        )

    async def _task(self) -> DeployProgressStatus:
        """Get current container states from ServiceStatus."""
        elapsed = int(time.time() - self.start_time)

        if not self.helm_values or not self.helm_values.services:
            self._running = False
            return DeployProgressStatus(
                services=[],
                elapsed_seconds=elapsed,
            )

        statuses = await self.status_watcher.get_service_statuses_for_deployment(
            skip_metrics=True
        )

        services: list[DeployServiceStatus] = []
        failure_detected = False
        failure_message: str | None = None

        for svc in statuses:
            deploy_svc = self._to_deploy_status(svc)
            services.append(deploy_svc)

            if deploy_svc.status in (
                StatusPhase.ERROR,
                StatusPhase.RESTARTING,
            ):
                failure_detected = True
                failure_message = f"{svc.name}: {deploy_svc.message}"

        return DeployProgressStatus(
            services=services,
            elapsed_seconds=elapsed,
            failure_detected=failure_detected,
            failure_message=failure_message,
        )

    def _to_deploy_status(self, svc: ServiceStatus) -> DeployServiceStatus:
        """Convert ServiceStatus to DeployServiceStatus using shared methods."""
        if svc.workload_type == WorkloadType.JOB:
            return self._job_to_deploy_status(svc)

        counts = svc.get_container_counts()
        phase = svc.get_deploy_phase()
        error_msg, _ = svc.get_error_info()
        message = self._phase_to_message(phase, error_msg)

        return DeployServiceStatus(
            name=svc.name,
            status=phase,
            containers=counts,
            message=message,
        )

    def _job_to_deploy_status(self, svc: ServiceStatus) -> DeployServiceStatus:
        """Convert Job ServiceStatus to DeployServiceStatus."""
        if svc.status == StatusPhase.EXITED:
            phase = StatusPhase.EXITED
            message = "Completed"
            counts = ContainerCounts(desired=1, running=0, stopping=0)
        elif svc.status == StatusPhase.ERROR:
            phase = StatusPhase.ERROR
            error_msg, _ = svc.get_error_info()
            message = error_msg or "Failed"
            counts = ContainerCounts(desired=1, running=0, stopping=0, error=1)
        elif svc.status == StatusPhase.RUNNING:
            phase = StatusPhase.RUNNING
            message = "Running"
            counts = ContainerCounts(desired=1, running=1, stopping=0)
        else:
            phase = StatusPhase.PENDING
            message = "Waiting to start"
            counts = ContainerCounts(desired=1, running=0, stopping=0)

        return DeployServiceStatus(
            name=svc.name,
            status=phase,
            containers=counts,
            message=message,
        )

    def _phase_to_message(self, phase: StatusPhase, error_msg: str | None) -> str:
        """Get user-friendly message for phase."""
        if error_msg:
            return error_msg
        if phase == StatusPhase.RUNNING:
            return "Healthy"
        if phase == StatusPhase.CREATING:
            return "Pulling image"
        if phase == StatusPhase.HEALTH_CHECK:
            return "Running health checks"
        if phase == StatusPhase.UPDATING:
            return "Updating"
        if phase == StatusPhase.STOPPING:
            return "Stopping"
        if phase == StatusPhase.PENDING:
            return "Waiting to start"
        if phase == StatusPhase.EXITED:
            return "Completed"
        if phase == StatusPhase.RESTARTING:
            return "Service keeps crashing"
        return ""
