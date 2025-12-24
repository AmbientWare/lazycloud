from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel

from models.helm import (
    CurrentUsage,
    HealthCheckValues,
    HPAValues,
)
from models.k8s import Resources, WorkloadType

JOB_CONDITION_COMPLETE = "Complete"
JOB_CONDITION_FAILED = "Failed"


class TaskStatus(StrEnum):
    """Status enumeration."""

    COMPLETED = "completed"
    PENDING = "pending"
    ERROR = "error"


class KubernetesPhase(StrEnum):
    """Status enumeration."""

    RUNNING = "Running"
    PARTIALLY_RUNNING = "Partially Running"
    PENDING = "Pending"
    SUCCEEDED = "Succeeded"
    FAILED = "Failed"
    STOPPED = "Stopped"
    TERMINATING = "Terminating"
    ERROR = "Error"
    UNKNOWN = "Unknown"


class DeployServicePhase(StrEnum):
    """Status phases for deploy progress - compose-friendly."""

    PENDING = "pending"
    STARTING = "starting"
    RUNNING = "running"
    RESTARTING = "restarting"
    ERROR = "error"
    EXITED = "exited"


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
    phase: KubernetesPhase
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
    status: KubernetesPhase
    replicas: int = 1
    ready_replicas: int = 0
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


class ServiceStatusSummary(BaseModel):
    """Simplified service status for UI display."""

    name: str
    status: KubernetesPhase
    ready_replicas: int
    total_replicas: int
    image: str | None = None
    ports: list[str] | None = None
    restarts: int = 0
    endpoint: str | None = None


class DeploymentStatus(BaseModel):
    """Status information for a deployment - optimized for UI display."""

    deployment_id: str
    deployment_name: str
    namespace: str
    status: KubernetesPhase
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
