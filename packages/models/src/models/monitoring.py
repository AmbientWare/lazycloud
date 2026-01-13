from enum import StrEnum

from models.statuses import DeployServicePhase
from pydantic import BaseModel, Field


class StreamEventType(StrEnum):
    """Event types for SSE streams."""

    STATUS = "status"
    LOG = "log"
    ERROR = "error"
    DEPLOY_PROGRESS = "deploy_progress"


class DeployOverallPhase(StrEnum):
    """Overall deployment progress phases."""

    DEPLOYING = "deploying"
    COMPLETED = "completed"
    FAILED = "failed"


class ContainerCounts(BaseModel):
    """Container counts for a service (Docker-like semantics)."""

    desired: int = 0
    running: int = 0
    pending: int = 0
    stopping: int = 0
    creating: int = 0  # Alias: starting (pulling image, initializing, health checks)
    error: int = 0


class DeployServiceStatus(BaseModel):
    """Status of a single service during deployment."""

    name: str
    status: DeployServicePhase
    ready: bool
    containers: ContainerCounts
    message: str
    last_log: str | None = None


class DeployProgressStatus(BaseModel):
    """Overall deployment progress status."""

    services: list[DeployServiceStatus]
    overall: DeployOverallPhase
    elapsed_seconds: int
    failure_detected: bool = False
    failure_message: str | None = None


class MonitorStats(BaseModel):
    """Statistics for a single monitor."""

    subscribers: int = Field(..., description="Number of active subscribers")
    running: bool = Field(..., description="Whether the monitor is currently running")


class SubscriptionManagerStats(BaseModel):
    """Statistics about the subscription manager state."""

    active_monitors: int = Field(..., description="Total number of active monitors")
    total_subscriptions: int = Field(
        ..., description="Total number of active subscriptions across all monitors"
    )
    pending_cleanups: int = Field(
        ..., description="Number of monitors scheduled for cleanup"
    )
    monitors: dict[str, MonitorStats] = Field(
        default_factory=dict,
        description="Per-monitor statistics keyed by monitor hash",
    )
