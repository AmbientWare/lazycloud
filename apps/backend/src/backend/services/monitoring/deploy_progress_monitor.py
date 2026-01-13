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
from models.pod_states import (
    ContainerState,
    PodInfo,
    PodStateClassifier,
)
from models.statuses import DeployServicePhase, KubernetesPhase, ServiceStatus

from backend.services.k8s.status_watcher import StatusWatcher
from backend.services.monitoring.base import BaseMonitor

# Mapping from ContainerState to DeployServicePhase
CONTAINER_STATE_TO_DEPLOY_PHASE: dict[ContainerState, DeployServicePhase] = {
    ContainerState.PENDING: DeployServicePhase.PENDING,
    ContainerState.STARTING: DeployServicePhase.STARTING,
    ContainerState.RUNNING: DeployServicePhase.RUNNING,
    ContainerState.UPDATING: DeployServicePhase.UPDATING,
    ContainerState.RESTARTING: DeployServicePhase.RESTARTING,
    ContainerState.STOPPING: DeployServicePhase.STOPPING,
    ContainerState.EXITED: DeployServicePhase.EXITED,
    ContainerState.ERROR: DeployServicePhase.ERROR,
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
        self._classifier = PodStateClassifier()

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
        """Convert to deploy status using PodStateClassifier."""
        # Build PodInfo list from ServiceStatus.pods
        pod_infos = [
            PodInfo(
                name=p.name,
                phase=p.phase.value if isinstance(p.phase, KubernetesPhase) else p.phase,
                reason=p.reason,
                message=p.message,
                ready_containers=p.ready_containers,
                total_containers=p.total_containers,
                restart_count=p.restart_count,
                is_terminating=(p.phase == KubernetesPhase.TERMINATING),
            )
            for p in (svc.pods or [])
        ]

        # Use classifier for service-level classification
        result = self._classifier.classify_service(
            pods=pod_infos,
            desired_replicas=svc.replicas,
            updated_replicas=svc.updated_replicas,
        )

        # Map ContainerState to DeployServicePhase
        phase = CONTAINER_STATE_TO_DEPLOY_PHASE.get(
            result.state, DeployServicePhase.PENDING
        )

        # Convert classifier counts to monitoring ContainerCounts
        counts = ContainerCounts(
            desired=result.container_counts.desired,
            running=result.container_counts.running,
            pending=result.container_counts.pending,
            stopping=result.container_counts.stopping,
            creating=result.container_counts.starting,  # Map 'starting' to 'creating' for API compat
            error=result.container_counts.error,
        )

        return DeployServiceStatus(
            name=svc.name,
            status=phase,
            ready=result.is_ready,
            containers=counts,
            message=result.message,
        )
