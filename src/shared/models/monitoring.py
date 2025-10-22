from enum import StrEnum

from pydantic import BaseModel, Field


class StreamEventType(StrEnum):
    """Event types for SSE streams."""

    STATUS = "status"
    LOG = "log"
    ERROR = "error"


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
