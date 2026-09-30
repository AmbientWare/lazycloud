from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from shared.autoscaler_state import AutoscalerStateRecord, AutoscalerTargetKind
from shared.containers import ContainerStatus


@dataclass(frozen=True, slots=True)
class AutoscalerStatus:
    state: AutoscalerStateRecord
    stub_name: str
    stub_kind: str
    autoscaling_enabled: bool


@dataclass(frozen=True, slots=True)
class AutoscalingContainer:
    id: str
    stub_id: str | None
    status: ContainerStatus
    runtime_worker_id: str
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    startup_error: str
    hot_reload: str | None
    rollout_serving_floor: int | None


@dataclass(frozen=True, slots=True)
class AutoscalingTargetClaim:
    stub_id: str
    workspace_id: str
    target_kind: AutoscalerTargetKind
    generation: int
    token: str
    due_at: datetime


__all__ = ["AutoscalerStatus", "AutoscalingContainer", "AutoscalingTargetClaim"]
