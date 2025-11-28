"""Deploy progress monitor - shows actual container states."""

import time
from typing import Callable

from models.helm import HelmValues
from models.monitoring import (
    ContainerCounts,
    DeployOverallPhase,
    DeployProgressStatus,
    DeployServiceStatus,
)
from models.statuses import DeployServicePhase, KubernetesPhase, ServiceStatus

from backend.services.k8s.status_watcher import StatusWatcher
from backend.services.monitoring.base import BaseMonitor

FAILURE_REASONS = {
    "CrashLoopBackOff",
    "ImagePullBackOff",
    "ErrImagePull",
    "ErrImageNeverPull",
    "InvalidImageName",
    "CreateContainerConfigError",
    "RunContainerError",
}


class DeployProgressMonitor(BaseMonitor[DeployProgressStatus]):
    """Monitors deployment - shows actual container states."""

    def __init__(
        self,
        deployment_id: str,
        deployment_name: str,
        namespace: str,
        helm_values: HelmValues,
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
            deployment_name=deployment_name,
        )

    async def _task(self) -> DeployProgressStatus:
        """Get current container states."""
        elapsed = int(time.time() - self.start_time)

        if not self.helm_values or not self.helm_values.services:
            return DeployProgressStatus(
                services=[],
                overall=DeployOverallPhase.COMPLETED,
                elapsed_seconds=elapsed,
            )

        service_statuses = (
            await self.status_watcher.get_service_statuses_for_deployment()
        )

        services: list[DeployServiceStatus] = []
        all_ready = True
        failure_detected = False
        failure_message: str | None = None

        for svc in service_statuses:
            deploy_svc = self._to_deploy_status(svc)
            services.append(deploy_svc)

            if not deploy_svc.ready:
                all_ready = False

            if deploy_svc.status in (
                DeployServicePhase.ERROR,
                DeployServicePhase.RESTARTING,
            ):
                failure_detected = True
                failure_message = f"{svc.name}: {deploy_svc.message}"

        if failure_detected:
            overall = DeployOverallPhase.FAILED
        elif all_ready:
            overall = DeployOverallPhase.COMPLETED
        else:
            overall = DeployOverallPhase.DEPLOYING

        return DeployProgressStatus(
            services=services,
            overall=overall,
            elapsed_seconds=elapsed,
            failure_detected=failure_detected,
            failure_message=failure_message,
        )

    def _to_deploy_status(self, svc: ServiceStatus) -> DeployServiceStatus:
        """Convert to deploy status with container counts."""
        counts = self._count_containers(svc)
        status, message = self._get_status_and_message(svc, counts)

        # Ready when running matches desired and no issues
        is_ready = (
            counts.running >= counts.desired
            and counts.desired > 0
            and counts.pending == 0
            and counts.creating == 0
            and counts.stopping == 0
        )

        try:
            phase = DeployServicePhase(status)
        except ValueError:
            phase = DeployServicePhase.PENDING

        return DeployServiceStatus(
            name=svc.name,
            status=phase,
            ready=is_ready,
            containers=counts,
            message=message,
        )

    def _count_containers(self, svc: ServiceStatus) -> ContainerCounts:
        """Count containers by state."""
        running = 0
        pending = 0
        stopping = 0
        creating = 0

        if svc.pods:
            for pod in svc.pods:
                if pod.reason == "ContainerCreating":
                    creating += 1
                elif pod.reason == "PodInitializing":
                    creating += 1
                elif pod.phase == KubernetesPhase.TERMINATING:
                    stopping += 1
                elif pod.phase == KubernetesPhase.RUNNING:
                    running += 1
                elif pod.phase == KubernetesPhase.PENDING:
                    pending += 1

        return ContainerCounts(
            desired=svc.replicas,
            running=running,
            pending=pending,
            stopping=stopping,
            creating=creating,
        )

    def _get_status_and_message(
        self, svc: ServiceStatus, counts: ContainerCounts
    ) -> tuple[str, str]:
        """Get status and message based on container states."""
        # Check for errors first
        if svc.pods:
            for pod in svc.pods:
                if pod.reason == "CrashLoopBackOff":
                    return "restarting", "Container keeps crashing"
                if pod.reason in ("ImagePullBackOff", "ErrImagePull"):
                    return "error", "Failed to pull image"
                if pod.reason in FAILURE_REASONS:
                    return "error", pod.message or pod.reason

        # Status based on counts
        if counts.creating > 0:
            return "starting", "Creating containers"

        if counts.stopping > 0:
            return "starting", "Replacing containers"

        if counts.pending > 0:
            if counts.running > 0:
                return "starting", "Scaling up"
            return "pending", "Waiting to start"

        if counts.running >= counts.desired and counts.desired > 0:
            return "running", "Healthy"

        if counts.running > 0:
            return "starting", "Starting"

        if counts.desired == 0:
            return "exited", "Stopped"

        return "pending", "Waiting"
