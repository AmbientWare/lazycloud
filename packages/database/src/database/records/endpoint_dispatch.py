from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True, slots=True)
class EndpointDispatchStateRecord:
    task_id: str
    workspace_id: str
    stub_id: str
    container_id: str | None
    method: str
    path: str
    status: str
    wait_timeout_seconds: float
    max_pending_requests: int
    max_inflight_per_container: int
    attempts: int
    enqueued_at: datetime
    started_at: datetime | None
    heartbeat_at: datetime | None
    expires_at: datetime
    finished_at: datetime | None
    error: str | None


@dataclass(frozen=True, slots=True)
class EndpointDispatchObservationRecord:
    stub_id: str
    container_id: str
    active: bool
    finished_at: datetime | None


__all__ = ["EndpointDispatchObservationRecord", "EndpointDispatchStateRecord"]
