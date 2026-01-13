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
    "ContainerCannotRun",
    "DeadlineExceeded",
}

# Reasons that indicate memory/resource issues
RESOURCE_REASONS = {
    "OOMKilled",
    "Evicted",
}

# Reasons that indicate container exited (not a long-running service)
EXIT_REASONS = {
    "ContainerExited",
    "Completed",
    "Error",
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
            # Auto-stop since there's nothing to monitor
            self._running = False
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
            self._running = False
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
        """Count instances by state using Docker-like semantics."""
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
                    # Only count as running if service is actually ready
                    # (all containers passing health checks)
                    if (
                        pod.ready_containers >= pod.total_containers
                        and pod.total_containers > 0
                    ):
                        running += 1
                    else:
                        # Service started but not yet healthy
                        creating += 1
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
        """Get status and message based on service state."""
        # Check for errors first
        if svc.pods:
            for pod in svc.pods:
                # Restart loop
                if pod.reason == "CrashLoopBackOff":
                    return "restarting", "Service keeps crashing"

                # Image issues
                if pod.reason in ("ImagePullBackOff", "ErrImagePull"):
                    return "error", "Failed to pull image"

                # Memory/resource issues
                if pod.reason in RESOURCE_REASONS:
                    if pod.reason == "OOMKilled":
                        return "error", "Out of memory - increase memory limit"
                    if pod.reason == "Evicted":
                        return "error", "Evicted due to resource pressure"
                    return "error", pod.message or pod.reason

                # Container exited (not a long-running service)
                if pod.reason in EXIT_REASONS:
                    return (
                        "error",
                        "Service exited - use restart: no for one-time tasks",
                    )

                # Other failures
                if pod.reason in FAILURE_REASONS:
                    return "error", pod.message or pod.reason

        # Check if any instances are running but not ready (health checks)
        has_unready_running = False
        if svc.pods:
            for pod in svc.pods:
                if (
                    pod.phase == KubernetesPhase.RUNNING
                    and pod.ready_containers < pod.total_containers
                ):
                    has_unready_running = True
                    break

        # Status based on counts and readiness
        if counts.creating > 0:
            # Rolling update: some instances healthy, some starting
            if counts.running > 0:
                # Check specific state for more detail
                if svc.pods:
                    for pod in svc.pods:
                        if pod.reason == "ContainerCreating":
                            return "starting", "Updating (pulling image)"
                        if pod.reason == "PodInitializing":
                            return "starting", "Updating (initializing)"
                if has_unready_running:
                    return "starting", "Updating (health checks)"
                return "starting", "Rolling update"

            # Fresh deploy: no instances running yet
            if svc.pods:
                for pod in svc.pods:
                    if pod.reason == "ContainerCreating":
                        return "starting", "Pulling image"
                    if pod.reason == "PodInitializing":
                        return "starting", "Initializing"

            # Running but waiting for health checks
            if has_unready_running:
                return "starting", "Running health checks"

            return "starting", "Starting service"

        if counts.stopping > 0:
            return "starting", "Updating service"

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
