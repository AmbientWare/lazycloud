"""Monitor configuration classes for type-safe subscription parameters."""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from uuid import UUID

from shared.models.helm import HelmValues


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
    namespace: str
    helm_values: HelmValues

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
    service_name: str
    namespace: str
    helm_values: HelmValues

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


# Type alias for all monitor configs (for type hints)
MonitorConfig = DeploymentMonitorConfig | ServiceMonitorConfig | TaskMonitorConfig
