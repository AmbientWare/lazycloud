from __future__ import annotations

from enum import Enum


class AppLifecycleState(str, Enum):
    Active = "active"
    Pausing = "pausing"
    Paused = "paused"
    Resuming = "resuming"
    Deleting = "deleting"
    CleanupFailed = "cleanup_failed"
    Deleted = "deleted"


class AppLifecycleTarget(str, Enum):
    Active = "active"
    Paused = "paused"
    Deleted = "deleted"


class AppDeploymentIntentTarget(str, Enum):
    Active = "active"
    Inactive = "inactive"
    Deleted = "deleted"


UNFINISHED_APP_LIFECYCLE_STATES = frozenset(
    {
        AppLifecycleState.Pausing,
        AppLifecycleState.Resuming,
        AppLifecycleState.Deleting,
        AppLifecycleState.CleanupFailed,
    }
)


__all__ = [
    "UNFINISHED_APP_LIFECYCLE_STATES",
    "AppDeploymentIntentTarget",
    "AppLifecycleState",
    "AppLifecycleTarget",
]
