from __future__ import annotations

from datetime import datetime

from pydantic import Field, JsonValue

from shared.contracts import ContractModel
from shared.timestamps import utc_now

TASK_EVENT_RESOURCE_TYPE = "task"
CONTAINER_EVENT_RESOURCE_TYPE = "container"

GATEWAY_REQUEST_EVENT_ACTION = "gateway.request"
ENDPOINT_SCALE_DECISION_ACTION = "endpoint.autoscaler.scale_decision"
POD_SCALE_DECISION_ACTION = "pod.autoscaler.scale_decision"
WORKER_POOL_SIZER_DECISION_ACTION = "worker_pool.sizer.decision"
WORKER_POOL_DRAIN_DECISION_ACTION = "worker_pool.drain.decision"

AUTOSCALER_SCALE_DECISION_ACTIONS = frozenset(
    {
        ENDPOINT_SCALE_DECISION_ACTION,
        POD_SCALE_DECISION_ACTION,
    }
)

# Control-loop and per-request telemetry actions: emitted continuously while the
# platform runs, useful for short-window diagnosis, and pruned on a much shorter
# retention than audit events.
TELEMETRY_EVENT_ACTIONS = frozenset(
    {
        GATEWAY_REQUEST_EVENT_ACTION,
        WORKER_POOL_SIZER_DECISION_ACTION,
        WORKER_POOL_DRAIN_DECISION_ACTION,
    }
    | AUTOSCALER_SCALE_DECISION_ACTIONS
)


class WorkerEventFilter(ContractModel):
    worker_id: str | None = None
    event_type: str | None = None
    resource_id: str | None = None
    since: datetime | None = None


class WorkerEventRecord(ContractModel):
    id: str
    worker_id: str
    event_type: str
    resource_id: str | None = None
    payload: dict[str, JsonValue] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utc_now)
