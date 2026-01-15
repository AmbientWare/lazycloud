from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel

from models.helm import (
    CurrentUsage,
    HealthCheckValues,
    HPAValues,
)
from models.k8s import Resources, WorkloadType
from models.pod_states import ContainerCounts, PodFailureReasons

JOB_CONDITION_COMPLETE = "Complete"
JOB_CONDITION_FAILED = "Failed"


class TaskStatus(StrEnum):
    """Status enumeration."""

    COMPLETED = "completed"
    PENDING = "pending"
    ERROR = "error"


class StatusPhase(StrEnum):
    """Unified status phases for pods, services, and deployments.

    Used everywhere - internal state and UI display.
    """

    PENDING = "pending"  # Waiting to be scheduled
    CREATING = "creating"  # Pulling image, creating container
    HEALTH_CHECK = "health_check"  # Container running, readiness probe pending
    RUNNING = "running"  # Fully healthy, serving traffic
    UPDATING = "updating"  # Rolling update in progress
    STOPPING = "stopping"  # Terminating gracefully
    RESTARTING = "restarting"  # CrashLoopBackOff
    ERROR = "error"  # Failed (unrecoverable)
    EXITED = "exited"  # Job completed or stopped (replicas=0)
    UNKNOWN = "unknown"  # Can't determine state


class StorageType(StrEnum):
    """Storage class types."""

    STANDARD = "Standard"
    SHARED = "Shared"


class VolumeStatus(BaseModel):
    """Status information for a volume."""

    name: str
    status: str
    mount_path: str | None = None
    size: str | None = None
    storage_type: StorageType = StorageType.STANDARD


class VolumeStatusSummary(BaseModel):
    """Simplified volume status for UI display."""

    name: str
    status: str
    storage_type: StorageType = StorageType.STANDARD


class NetworkStatus(BaseModel):
    """Status information for a network."""

    name: str
    status: str
    driver: str | None = None


class NetworkStatusSummary(BaseModel):
    """Simplified network status for UI display."""

    name: str
    status: str
    driver: str | None = None


class PodStatus(BaseModel):
    """Status information for a single pod/instance."""

    name: str
    phase: StatusPhase
    ready_containers: int
    total_containers: int
    restart_count: int
    age: str
    node: str
    ip: str | None = None
    cpu_usage: str | None = None
    memory_usage: str | None = None
    reason: str | None = None
    message: str | None = None


class ServiceStatus(BaseModel):
    """Service status information from Kubernetes."""

    name: str
    image: str
    workload_type: WorkloadType
    status: StatusPhase
    replicas: int = 1
    ready_replicas: int = 0
    updated_replicas: int | None = None
    pods: list[PodStatus] | None = None
    resources: Resources | None = None
    current_usage: CurrentUsage | None = None
    ports: list[str] | None = None  # NOTE: Format: "8080:8080/TCP"
    volumes: list[str] | None = None
    hpa: HPAValues | None = None
    healthcheck: HealthCheckValues | None = None
    total_restarts: int = 0
    last_checked: datetime
    endpoint: str | None = None
    # Custom domain fields
    custom_domain: str | None = None
    domain_status: str | None = None
    cname_target: str | None = None

    def get_container_counts(self) -> ContainerCounts:
        """Get Docker-like container counts from pods."""
        running = creating = health_check = pending = stopping = error = 0

        for pod in self.pods or []:
            if pod.phase == StatusPhase.STOPPING:
                stopping += 1
            elif pod.phase == StatusPhase.RUNNING:
                running += 1
            elif pod.phase == StatusPhase.CREATING:
                creating += 1
            elif pod.phase == StatusPhase.HEALTH_CHECK:
                health_check += 1
            elif pod.phase == StatusPhase.PENDING:
                pending += 1
            elif pod.phase in (StatusPhase.ERROR, StatusPhase.RESTARTING):
                error += 1

        return ContainerCounts(
            desired=self.replicas,
            running=running,
            creating=creating,
            health_check=health_check,
            pending=pending,
            stopping=stopping,
            error=error,
        )

    def get_error_info(self) -> tuple[str | None, str | None]:
        """Get error message and reason from first errored pod."""
        for pod in self.pods or []:
            if pod.phase == StatusPhase.ERROR and pod.message:
                return pod.message, pod.reason
        return None, None

    def get_deploy_phase(self) -> StatusPhase:
        """Get Docker-like deploy phase for display."""
        counts = self.get_container_counts()
        _, error_reason = self.get_error_info()

        # 1. Error takes precedence
        if error_reason:
            if error_reason in PodFailureReasons.RESTART_ERRORS:
                return StatusPhase.RESTARTING
            return StatusPhase.ERROR

        # 2. Rolling update detected via updated_replicas
        if self.updated_replicas is not None and self.updated_replicas < self.replicas:
            return StatusPhase.UPDATING

        # 3. Stopping (with some running = UPDATING)
        if counts.stopping > 0:
            if counts.running > 0 or counts.creating > 0 or counts.health_check > 0:
                return StatusPhase.UPDATING
            return StatusPhase.STOPPING

        # 4. Health check pending (container running but readiness probe not passed)
        if counts.health_check > 0:
            return StatusPhase.HEALTH_CHECK

        # 5. Creating (pulling image, creating container)
        if counts.creating > 0:
            return StatusPhase.CREATING

        # 6. Pending pods
        if counts.pending > 0:
            return StatusPhase.PENDING

        # 7. All running = RUNNING (only when no pods in transitional states)
        if counts.running >= counts.desired and counts.desired > 0:
            return StatusPhase.RUNNING

        return StatusPhase.PENDING


class ServiceStatusSummary(BaseModel):
    """Simplified service status for UI display."""

    name: str
    status: StatusPhase
    ready_replicas: int
    total_replicas: int
    image: str | None = None
    ports: list[str] | None = None
    restarts: int = 0
    endpoint: str | None = None

    # Custom domain fields
    custom_domain: str | None = None  # The custom domain if configured
    domain_status: str | None = None  # "pending_validation" | "active" | None
    cname_target: str | None = None  # Target for CNAME record when pending

    # Detailed fields for service details view
    resources: Resources | None = None
    current_usage: CurrentUsage | None = None
    healthcheck: HealthCheckValues | None = None
    hpa: HPAValues | None = None
    pods: list["PodStatus"] | None = None


class DeploymentStatus(BaseModel):
    """Status information for a deployment - optimized for UI display."""

    deployment_id: str
    deployment_name: str
    namespace: str
    status: StatusPhase
    ready: bool
    last_checked: datetime
    deployed_at: datetime | None = None

    # Summary counts
    total_services: int
    ready_services: int
    total_replicas: int
    ready_replicas: int

    # NOTE: Only summary data for less data transfer
    services: list[ServiceStatusSummary]
    volumes: list[VolumeStatusSummary] | None = None
    networks: list[NetworkStatusSummary] | None = None
