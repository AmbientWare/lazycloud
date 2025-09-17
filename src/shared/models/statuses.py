from datetime import datetime
from enum import StrEnum
from typing import List, Optional

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
    PENDING = "Pending"
    STOPPED = "Stopped"
    ERROR = "Error"


class PodStatus(BaseModel):
    """Status information for a single pod/instance."""

    name: str
    phase: KubernetesPhase
    ready_containers: int
    total_containers: int
    restart_count: int
    age: str
    node: str
    ip: Optional[str] = None
    cpu_usage: Optional[str] = None
    memory_usage: Optional[str] = None


class ServiceStatus(BaseModel):
    """Service status information from Kubernetes."""

    name: str
    image: str
    workload_type: WorkloadType  # Deployment, StatefulSet
    status: KubernetesPhase  # running, pending, stopped, error
    replicas: int = 1
    ready_replicas: int = 0
    pods: Optional[List[PodStatus]] = None
    resources: Optional[Resources] = None
    current_usage: Optional[CurrentUsage] = None
    ports: Optional[List[str]] = None  # Format: "8080:8080/TCP"
    volumes: Optional[List[str]] = None
    hpa: Optional[HPAValues] = None
    healthcheck: Optional[HealthCheckValues] = None
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
