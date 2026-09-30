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


__all__ = [
    "AppLifecycleState",
]
