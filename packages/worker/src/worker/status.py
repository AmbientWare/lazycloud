from __future__ import annotations

from enum import StrEnum

from pydantic import Field, JsonValue
from shared.contracts import ContractModel
from shared.scheduling import SchedulerContainerStatus

CONTAINER_STATE_TTL_SECONDS = 120
CONTAINER_STATE_TTL_WHILE_PENDING_SECONDS = 600
DEFAULT_WORKER_SPINDOWN_SECONDS = 300.0
DEFAULT_WORKER_STOP_GRACE_SECONDS = 30
SHUTDOWN_DRAIN_MAX_SECONDS = 5.0
SHUTDOWN_FORCE_WAIT_SECONDS = 5.0
SHUTDOWN_CLEANUP_RESERVE_SECONDS = 5.0
WORKER_ORPHAN_STATE_MISSING_EVENT_ID = "worker.orphan_state_missing"
WORKER_PENDING_RECONCILED_EVENT_ID = "worker.pending_reconciled_running"
WORKER_STOPPING_GRACE_KILL_EVENT_ID = "worker.stopping_grace_kill"


class WorkerContainerStatus(StrEnum):
    Pending = "pending"
    Running = "running"
    Stopping = "stopping"
    Stopped = "stopped"
    Unknown = "unknown"


class WorkerStopReason(StrEnum):
    Ttl = "TTL"
    User = "USER"
    Scheduler = "SCHEDULER"
    Preempted = "PREEMPTED"
    Admin = "ADMIN"
    Unknown = "UNKNOWN"


class WorkerStatusHeartbeatAction(StrEnum):
    StopHeartbeat = "stop-heartbeat"
    UpdateStatus = "update-status"
    StopOrphan = "stop-orphan"
    Error = "error"


class WorkerSchedulerRequestAction(StrEnum):
    """What the worker did with one pass over its request queue.

    One enum for the whole pass rather than one for the plan and one for the
    outcome: the plan names the subset that follows from the container's state,
    and a second copy of those members is a translation table between two names
    for the same decision.
    """

    Idle = "idle"
    Execute = "execute"
    DropMissingState = "drop-missing-state"
    DropStoppingState = "drop-stopping-state"
    DropFinishedState = "drop-finished-state"
    SkipStartedContainer = "skip-started-container"
    ReconcileDelivery = "reconcile-delivery"


class WorkerSpindownAction(StrEnum):
    Continue = "continue"
    Shutdown = "shutdown"


class WorkerStatusHeartbeatPlan(ContractModel):
    action: WorkerStatusHeartbeatAction
    done: bool = False
    next_status: WorkerContainerStatus = WorkerContainerStatus.Unknown
    update_status: bool = False
    expiry_seconds: int = CONTAINER_STATE_TTL_SECONDS
    stop_container: bool = False
    kill: bool = False
    schedule_grace_kill: bool = False
    grace_seconds: int = DEFAULT_WORKER_STOP_GRACE_SECONDS
    stop_reason: WorkerStopReason = WorkerStopReason.Unknown
    event_id: str = ""
    event_attrs: dict[str, JsonValue] = Field(default_factory=dict)
    error_message: str = ""
    reason: str = ""


class WorkerDeliveredRequestPlan(ContractModel):
    """What follows from the action `plan_delivered_container_request` chose.

    Everything but the action and the reason is that action read back, so nothing
    can be constructed holding a consequence its action does not have.
    """

    action: WorkerSchedulerRequestAction
    reason: str = ""

    @property
    def drop(self) -> bool:
        return self.action is not WorkerSchedulerRequestAction.Execute

    @property
    def delete_state(self) -> bool:
        return self.action is WorkerSchedulerRequestAction.DropStoppingState

    @property
    def release_capacity(self) -> bool:
        """Whether this path hands the container's reservation back.

        Only where nothing ever held it. A container that is running or has
        finished had its reservation released by the execution that held it, and
        releasing again here would hand the worker capacity it never got back.
        """

        return self.action in {
            WorkerSchedulerRequestAction.DropMissingState,
            WorkerSchedulerRequestAction.DropStoppingState,
        }


class WorkerSpindownPlan(ContractModel):
    action: WorkerSpindownAction
    should_shutdown: bool
    cleanup_workspace_storage: bool = False
    cancel_worker_context: bool = False
    reason: str = ""


class WorkerShutdownBudgetPlan(ContractModel):
    configured_seconds: int
    budget_seconds: float
    drain_timeout_seconds: float
    stop_grace_seconds: float
    force_wait_seconds: float = SHUTDOWN_FORCE_WAIT_SECONDS
    cleanup_reserve_seconds: float = SHUTDOWN_CLEANUP_RESERVE_SECONDS


def normalize_worker_container_status(
    status: WorkerContainerStatus | str | None,
) -> WorkerContainerStatus:
    if isinstance(status, WorkerContainerStatus):
        return status
    normalized = (status or "").strip().lower()
    for item in WorkerContainerStatus:
        if normalized == item.value:
            return item
    return WorkerContainerStatus.Unknown


def plan_worker_status_heartbeat(
    *,
    instance_exists: bool = True,
    exit_code: int = -1,
    state_status: WorkerContainerStatus | str | None = WorkerContainerStatus.Running,
    state_missing: bool = False,
    runtime_started: bool = False,
    runtime_pid: int = 0,
    stop_reason: WorkerStopReason | str = WorkerStopReason.Unknown,
    termination_grace_seconds: int = DEFAULT_WORKER_STOP_GRACE_SECONDS,
) -> WorkerStatusHeartbeatPlan:
    if not instance_exists:
        return WorkerStatusHeartbeatPlan(
            action=WorkerStatusHeartbeatAction.StopHeartbeat,
            done=True,
            reason="container instance is no longer tracked",
        )
    if exit_code >= 0:
        return WorkerStatusHeartbeatPlan(
            action=WorkerStatusHeartbeatAction.StopHeartbeat,
            done=True,
            reason="container already exited",
        )
    if state_missing:
        return WorkerStatusHeartbeatPlan(
            action=WorkerStatusHeartbeatAction.StopOrphan,
            done=True,
            stop_container=True,
            kill=True,
            stop_reason=WorkerStopReason.Unknown,
            event_id=WORKER_ORPHAN_STATE_MISSING_EVENT_ID,
            reason="container state is missing",
        )
    if state_status is None:
        return WorkerStatusHeartbeatPlan(
            action=WorkerStatusHeartbeatAction.Error,
            error_message="container state response missing state",
            reason="missing state",
        )

    status = normalize_worker_container_status(state_status)
    next_status = status
    expiry = CONTAINER_STATE_TTL_SECONDS
    event_id = ""
    event_attrs: dict[str, JsonValue] = {}
    reason = "container status refreshed"
    if status is WorkerContainerStatus.Pending:
        if runtime_started:
            next_status = WorkerContainerStatus.Running
            event_id = WORKER_PENDING_RECONCILED_EVENT_ID
            event_attrs = {"runtime_pid": runtime_pid}
            reason = "pending container reconciled to running"
        else:
            expiry = CONTAINER_STATE_TTL_WHILE_PENDING_SECONDS
            reason = "pending container heartbeat"

    schedule_grace_kill = status is WorkerContainerStatus.Stopping
    if schedule_grace_kill:
        reason = "stopping container heartbeat"

    return WorkerStatusHeartbeatPlan(
        action=WorkerStatusHeartbeatAction.UpdateStatus,
        next_status=next_status,
        update_status=True,
        expiry_seconds=expiry,
        schedule_grace_kill=schedule_grace_kill,
        grace_seconds=_positive_or_default(
            termination_grace_seconds,
            DEFAULT_WORKER_STOP_GRACE_SECONDS,
        ),
        kill=schedule_grace_kill,
        stop_reason=_normalize_stop_reason(stop_reason),
        event_id=event_id or (WORKER_STOPPING_GRACE_KILL_EVENT_ID if schedule_grace_kill else ""),
        event_attrs=event_attrs,
        reason=reason,
    )


def plan_delivered_container_request(
    *,
    state_status: SchedulerContainerStatus | None = None,
    state_missing: bool = False,
) -> WorkerDeliveredRequestPlan:
    """What the worker should do with a request it has just been handed.

    Delivery is at least once, so this has to answer for a request the worker has
    already acted on as well as for a fresh one, and the container's own state is
    what separates them. A dispatch writes `pending` before the request is queued
    and only the worker that took it writes anything later, so `running` means
    this worker already started it and a terminal status means it already ran.
    Neither may run again, and neither may hand capacity back a second time: the
    reservation for that container was made once and is released once by the
    execution that actually holds it.
    """

    if state_missing:
        return WorkerDeliveredRequestPlan(
            action=WorkerSchedulerRequestAction.DropMissingState,
            reason="container state is missing",
        )
    if state_status is SchedulerContainerStatus.Stopping:
        return WorkerDeliveredRequestPlan(
            action=WorkerSchedulerRequestAction.DropStoppingState,
            reason="container state is already stopping",
        )
    if state_status is SchedulerContainerStatus.Running:
        return WorkerDeliveredRequestPlan(
            action=WorkerSchedulerRequestAction.SkipStartedContainer,
            reason="container has already been started by this worker",
        )
    if state_status is SchedulerContainerStatus.Complete or (
        state_status is SchedulerContainerStatus.Failed
    ):
        return WorkerDeliveredRequestPlan(
            action=WorkerSchedulerRequestAction.DropFinishedState,
            reason=f"container has already finished as {state_status.value}",
        )
    return WorkerDeliveredRequestPlan(
        action=WorkerSchedulerRequestAction.Execute,
        reason="container request should run",
    )


def plan_mark_container_stopping() -> WorkerStatusHeartbeatPlan:
    return WorkerStatusHeartbeatPlan(
        action=WorkerStatusHeartbeatAction.UpdateStatus,
        next_status=WorkerContainerStatus.Stopping,
        update_status=True,
        expiry_seconds=CONTAINER_STATE_TTL_WHILE_PENDING_SECONDS,
        reason="mark container stopping",
    )


def plan_worker_spindown(
    *,
    context_cancelled: bool = False,
    persistent: bool = False,
    seconds_since_last_request: float = 0,
    active_container_count: int = 0,
    spindown_seconds: float = DEFAULT_WORKER_SPINDOWN_SECONDS,
) -> WorkerSpindownPlan:
    if context_cancelled:
        return WorkerSpindownPlan(
            action=WorkerSpindownAction.Shutdown,
            should_shutdown=True,
            reason="worker context cancelled",
        )
    if persistent:
        return WorkerSpindownPlan(
            action=WorkerSpindownAction.Continue,
            should_shutdown=False,
            reason="persistent worker stays available",
        )
    if seconds_since_last_request > spindown_seconds and active_container_count == 0:
        return WorkerSpindownPlan(
            action=WorkerSpindownAction.Shutdown,
            should_shutdown=True,
            cleanup_workspace_storage=True,
            cancel_worker_context=True,
            reason="worker idle past spindown threshold",
        )
    return WorkerSpindownPlan(
        action=WorkerSpindownAction.Continue,
        should_shutdown=False,
        reason="worker still active",
    )


def worker_shutdown_drain_timeout(configured_seconds: int) -> float:
    budget = _worker_shutdown_budget(configured_seconds)
    if budget <= 10:
        return 0.0
    return min(budget / 6, SHUTDOWN_DRAIN_MAX_SECONDS)


def worker_container_stop_grace(configured_seconds: int) -> float:
    budget = _worker_shutdown_budget(configured_seconds)
    grace = (
        budget
        - worker_shutdown_drain_timeout(configured_seconds)
        - SHUTDOWN_FORCE_WAIT_SECONDS
        - SHUTDOWN_CLEANUP_RESERVE_SECONDS
    )
    return budget if grace <= 0 else grace


def plan_worker_shutdown_budget(configured_seconds: int) -> WorkerShutdownBudgetPlan:
    budget = _worker_shutdown_budget(configured_seconds)
    return WorkerShutdownBudgetPlan(
        configured_seconds=configured_seconds,
        budget_seconds=budget,
        drain_timeout_seconds=worker_shutdown_drain_timeout(configured_seconds),
        stop_grace_seconds=worker_container_stop_grace(configured_seconds),
    )


def _worker_shutdown_budget(configured_seconds: int) -> float:
    return float(_positive_or_default(configured_seconds, DEFAULT_WORKER_STOP_GRACE_SECONDS))


def _positive_or_default(value: int, default: int) -> int:
    return value if value > 0 else default


def _normalize_stop_reason(value: WorkerStopReason | str) -> WorkerStopReason:
    if isinstance(value, WorkerStopReason):
        return value
    normalized = value.strip().upper()
    for reason in WorkerStopReason:
        if normalized == reason.value:
            return reason
    return WorkerStopReason.Unknown
