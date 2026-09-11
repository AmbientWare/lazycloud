from __future__ import annotations

from datetime import datetime

from pydantic import AwareDatetime

from shared.enums import StringEnum
from shared.http.base import HttpModel

PENDING_NOTICE_DELAY_SECONDS = 5
PENDING_PROGRESS_FRESH_SECONDS = 30
PENDING_PROGRESS_REFRESH_SECONDS = 10


class TaskPendingReason(StringEnum):
    Queued = "queued"
    Dependencies = "dependencies"
    Retry = "retry"
    CapacityBusy = "capacity_busy"
    CapacityUnavailable = "capacity_unavailable"
    CapacityLimit = "capacity_limit"
    ProvisioningCompute = "provisioning_compute"
    StartingContainer = "starting_container"


_MESSAGES: dict[TaskPendingReason, str] = {
    TaskPendingReason.Queued: "Waiting for this function to start.",
    TaskPendingReason.Dependencies: "Waiting for input functions to finish.",
    TaskPendingReason.Retry: "Waiting to retry this function.",
    TaskPendingReason.CapacityBusy: "Waiting for an available function container.",
    TaskPendingReason.CapacityUnavailable: (
        "Waiting for compute availability. Retrying automatically."
    ),
    TaskPendingReason.CapacityLimit: "Waiting for capacity within the compute limit.",
    TaskPendingReason.ProvisioningCompute: "Starting compute for this function.",
    TaskPendingReason.StartingContainer: "Compute is assigned. Starting the function container.",
}


class TaskPendingProgress(HttpModel):
    reason: TaskPendingReason
    message: str
    since: AwareDatetime
    pending_since: AwareDatetime
    observed_at: AwareDatetime

    @classmethod
    def for_reason(
        cls, reason: TaskPendingReason, *, since: datetime, observed_at: datetime
    ) -> TaskPendingProgress:
        return cls(
            reason=reason,
            message=_MESSAGES[reason],
            since=since,
            pending_since=since,
            observed_at=observed_at,
        )


__all__ = [
    "PENDING_NOTICE_DELAY_SECONDS",
    "PENDING_PROGRESS_FRESH_SECONDS",
    "PENDING_PROGRESS_REFRESH_SECONDS",
    "TaskPendingProgress",
    "TaskPendingReason",
]
