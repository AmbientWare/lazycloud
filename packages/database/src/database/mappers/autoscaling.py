from __future__ import annotations

from shared.autoscaler_state import (
    AutoscalerStateRecord,
    AutoscalerTargetKind,
    autoscaler_state_name,
)
from shared.timestamps import to_utc, to_utc_or_none

from database.tables.orchestration import AutoscalerStateTable


def autoscaler_state_from_row(row: AutoscalerStateTable) -> AutoscalerStateRecord:
    return AutoscalerStateRecord.model_validate(
        {
            "name": autoscaler_state_name(AutoscalerTargetKind(row.target_kind), row.target_id),
            "workspace_id": row.workspace_id,
            "source": row.source,
            "target_kind": row.target_kind,
            "target_id": row.target_id,
            "deployment_id": row.deployment_id,
            "app_id": row.app_id,
            "current_count": row.current_count,
            "desired_count": row.desired_count,
            "signal_name": row.signal_name,
            "signal_value": row.signal_value,
            "decision": row.decision,
            "reason": row.reason,
            "active": row.active,
            "valid": row.valid,
            "lock_acquired": row.lock_acquired,
            "owner_lock_key": row.owner_lock_key,
            "cooldown_until": to_utc_or_none(row.cooldown_until),
            "failed_container_count": row.failed_container_count,
            "pending_count": row.pending_count,
            "guardrails": row.guardrails,
            "last_actions": row.last_actions,
            "updated_at": to_utc(row.updated_at),
        }
    )
