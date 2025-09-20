from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel

from shared.models.helm import (
    CurrentUsage,
    HealthCheckValues,
    HPAValues,
)
from shared.models.k8s import Resources, WorkloadType


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
    ERROR = "Error"
    UNKNOWN = "Unknown"


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


class ServiceStatus(BaseModel):
    """Service status information from Kubernetes."""

    name: str
    image: str
    workload_type: WorkloadType  # Deployment, StatefulSet
    status: KubernetesPhase  # running, pending, stopped, error
    replicas: int = 1
    ready_replicas: int = 0
    pods: list[PodStatus] | None = None
    resources: Resources | None = None
    current_usage: CurrentUsage | None = None
    ports: list[str] | None = None  # Format: "8080:8080/TCP"
    volumes: list[str] | None = None
    hpa: HPAValues | None = None
    healthcheck: HealthCheckValues | None = None
    total_restarts: int = 0


class DeploymentStatus(BaseModel):
    """Status information for a deployment."""

    deployment_id: str
    deployment_name: str
    namespace: str
    services: list[ServiceStatus]
    volumes: dict[str, str] | None = None  # volume_name -> status
    networks: dict[str, str] | None = None  # network_name -> status
    status: KubernetesPhase  # running, partially running, stopped
    ready: bool
    last_updated: datetime
