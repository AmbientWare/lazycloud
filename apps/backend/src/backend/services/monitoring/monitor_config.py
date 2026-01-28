from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from models.helm import HelmValues


class MonitorConfigBase(ABC):
    """Abstract base class for all monitor configurations."""

    @abstractmethod
    def get_key(self) -> str:
        """
        Generate unique key for this monitor.

        This key must be unique per unique monitor instance and should include
        all parameters that distinguish one monitor from another.
        """
        pass

    @abstractmethod
    def get_monitor_type(self) -> str:
        """
        Get monitor type string for logging and identification.

        Should return a simple string like "deployment", "service", "task", etc.
        """
        pass


@dataclass(frozen=True)
class DeploymentMonitorConfig(MonitorConfigBase):
    """Configuration for a deployment monitor."""

    deployment_id: str
    deployment_name: str
    namespace: str
    helm_values: HelmValues
    deployed_at: datetime
    cluster_id: str

    def get_key(self) -> str:
        """Generate unique key for this monitor."""
        return f"deployment|{self.deployment_id}"

    def get_monitor_type(self) -> str:
        """Get monitor type string."""
        return "deployment"


@dataclass(frozen=True)
class ServiceMonitorConfig(MonitorConfigBase):
    """Configuration for a service monitor."""

    deployment_id: str
    deployment_name: str
    service_name: str
    namespace: str
    helm_values: HelmValues
    cluster_id: str

    def get_key(self) -> str:
        """Generate unique key for this monitor."""
        return f"service|{self.deployment_id}|{self.service_name}"

    def get_monitor_type(self) -> str:
        """Get monitor type string."""
        return "service"


@dataclass(frozen=True)
class TaskMonitorConfig(MonitorConfigBase):
    """Configuration for a task monitor."""

    task_id: UUID

    def get_key(self) -> str:
        """Generate unique key for this monitor."""
        return f"task|{self.task_id}"

    def get_monitor_type(self) -> str:
        """Get monitor type string."""
        return "task"


@dataclass(frozen=True)
class LogMonitorConfig(MonitorConfigBase):
    """Configuration for a log monitor."""

    deployment_id: str
    namespace: str
    service_name: str
    pod_name: str
    cluster_id: str
    tail_lines: int = 100

    def get_key(self) -> str:
        """Generate unique key for this monitor"""
        return f"logs|{self.deployment_id}|{self.namespace}|{self.pod_name}|{self.tail_lines}"

    def get_monitor_type(self) -> str:
        """Get monitor type string."""
        return "logs"


@dataclass(frozen=True)
class DeployProgressMonitorConfig(MonitorConfigBase):
    """Configuration for a deploy progress monitor with early failure detection."""

    deployment_id: str
    deployment_name: str
    namespace: str
    helm_values: HelmValues
    cluster_id: str

    def get_key(self) -> str:
        """Generate unique key for this monitor."""
        return f"deploy_progress|{self.deployment_id}"

    def get_monitor_type(self) -> str:
        """Get monitor type string."""
        return "deploy_progress"


# Type alias for all monitor configs (for type hints)
MonitorConfig = (
    DeploymentMonitorConfig
    | ServiceMonitorConfig
    | TaskMonitorConfig
    | LogMonitorConfig
    | DeployProgressMonitorConfig
)
