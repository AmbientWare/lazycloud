from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from shared.autoscaler_state import AutoscalerTargetKind


@dataclass(frozen=True, slots=True)
class AutoscalingTargetClaim:
    stub_id: str
    workspace_id: str
    target_kind: AutoscalerTargetKind
    generation: int
    token: str
    due_at: datetime


__all__ = ["AutoscalingTargetClaim"]
