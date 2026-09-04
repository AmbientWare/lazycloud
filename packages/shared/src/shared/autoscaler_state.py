from __future__ import annotations

from datetime import datetime

from pydantic import Field, JsonValue

from shared.contracts import ContractModel
from shared.deployments import StubKind
from shared.enums import StringEnum
from shared.timestamps import utc_now


class AutoscalerTargetKind(StringEnum):
    Function = "function"
    Endpoint = "endpoint"
    Pod = "pod"


class AutoscalerStateRecord(ContractModel):
    name: str
    workspace_id: str
    source: str
    target_kind: AutoscalerTargetKind
    target_id: str
    deployment_id: str = ""
    app_id: str = ""
    current_count: int = 0
    desired_count: int = 0
    signal_name: str = ""
    signal_value: int = 0
    decision: str = ""
    reason: str = ""
    active: bool = True
    valid: bool = True
    lock_acquired: bool = True
    owner_lock_key: str = ""
    cooldown_until: datetime | None = None
    failed_container_count: int = 0
    error: str = ""
    last_sample: dict[str, JsonValue] = Field(default_factory=dict)
    last_actions: list[dict[str, JsonValue]] = Field(default_factory=list)
    updated_at: datetime = Field(default_factory=utc_now)


def autoscaler_target_kind(kind: StubKind) -> AutoscalerTargetKind | None:
    if kind is StubKind.Function:
        return AutoscalerTargetKind.Function
    if kind in {StubKind.Endpoint, StubKind.Asgi}:
        return AutoscalerTargetKind.Endpoint
    if kind in {StubKind.Pod, StubKind.Sandbox}:
        return AutoscalerTargetKind.Pod
    return None


def autoscaler_state_name(target_kind: AutoscalerTargetKind, target_id: str) -> str:
    return f"{target_kind.value}:{target_id}"


__all__ = [
    "AutoscalerStateRecord",
    "AutoscalerTargetKind",
    "autoscaler_state_name",
    "autoscaler_target_kind",
]
