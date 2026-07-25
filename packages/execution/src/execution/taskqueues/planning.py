from __future__ import annotations

from enum import StrEnum

from pydantic import Field, computed_field
from shared.autoscaling import (
    TaskQueueAutoscalerSample,
    TaskQueueScaleDecision,
    TaskQueueScaleDecisionKind,
    TaskQueueScaleReason,
)
from shared.contracts import ContractModel
from shared.http.task_payload import HttpTaskPayload, serialize_http_task_payload
from shared.http.taskqueues import (
    DEFAULT_TASK_QUEUE_SERVE_TIMEOUT_SECONDS,
    TaskQueueTaskMessage,
)
from shared.tasks import TaskStatus, is_terminal_task_status
from shared.workload_keys import (
    task_queue_keep_warm_lock_key,
    task_queue_processing_lock_key,
    task_queue_running_lock_index_key,
    task_queue_running_lock_key,
)

from execution.config import ManagedPythonExecutable

DEFAULT_TASK_QUEUE_TASK_TTL_SECONDS = 7_200
DEFAULT_TASK_QUEUE_RUNNING_LOCK_TTL_SECONDS = 60
DEFAULT_TASK_QUEUE_MONITOR_HEARTBEAT_TTL_SECONDS = 60
DEFAULT_TASK_QUEUE_MONITOR_RUNNING_LOCK_TTL_SECONDS = 5
DEFAULT_TASK_QUEUE_MONITOR_POLL_INTERVAL_SECONDS = 1
TASK_QUEUE_WORKER_RUNNER_MODULE = "runner.taskqueue"
TASK_QUEUE_SERVE_RUNNER_MODULE = TASK_QUEUE_WORKER_RUNNER_MODULE
TASK_QUEUE_DEFAULT_PYTHON_EXECUTABLE = "python3.12"


class TaskQueueServeRequest(ContractModel):
    stub_id: str
    workspace_name: str = "default"
    timeout_seconds: int = Field(default=DEFAULT_TASK_QUEUE_SERVE_TIMEOUT_SECONDS, gt=0)
    python_executable: ManagedPythonExecutable = TASK_QUEUE_DEFAULT_PYTHON_EXECUTABLE
    runner_module: str = TASK_QUEUE_SERVE_RUNNER_MODULE


class TaskQueueServePlan(ContractModel):
    request: TaskQueueServeRequest
    entrypoint: list[str]
    serve_lock_key: str
    serve_lock_ttl_seconds: int
    wait_timeout_seconds: int

    @computed_field
    @property
    def authorized(self) -> bool:
        return True


class TaskQueueServeCompletionPlan(ContractModel):
    workspace_name: str
    stub_id: str
    container_id: str
    release_keep_warm_lock_key: str
    keep_serve_lock: bool = True


class TaskQueuePutStatus(StrEnum):
    Accepted = "accepted"
    TooManyPendingTasks = "too-many-pending-tasks"
    InvalidPayload = "invalid-payload"


class TaskQueuePutRequest(ContractModel):
    body: bytes = b""
    query_params: dict[str, list[str]] = Field(default_factory=dict)
    tasks_in_flight: int = Field(default=0, ge=0)
    max_pending_tasks: int = Field(default=1, ge=0)
    ttl_seconds: int = Field(default=0, ge=0)
    parse_payload: bool = True


class TaskQueuePutPlan(ContractModel):
    status: TaskQueuePutStatus
    accepted: bool
    payload: HttpTaskPayload | None = None
    ttl_seconds: int = DEFAULT_TASK_QUEUE_TASK_TTL_SECONDS
    reason: str = ""


class TaskQueuePopStatus(StrEnum):
    Empty = "empty"
    Claim = "claim"
    SkipCompleted = "skip-completed"


class TaskQueuePopRequest(ContractModel):
    workspace_name: str
    stub_id: str
    container_id: str
    queue_length: int = Field(default=0, ge=0)
    task_message: TaskQueueTaskMessage | None = None
    task_status: TaskStatus | None = None


class TaskQueuePopPlan(ContractModel):
    status: TaskQueuePopStatus
    queue_key: str
    processing_lock_key: str
    task_message: TaskQueueTaskMessage | None = None
    claim_task: bool = False
    cleanup_completed_task: bool = False
    running_lock_index_key: str | None = None
    running_lock_key: str | None = None
    heartbeat_key: str | None = None
    running_lock_ttl_seconds: int = DEFAULT_TASK_QUEUE_RUNNING_LOCK_TTL_SECONDS
    heartbeat_ttl_seconds: int = DEFAULT_TASK_QUEUE_MONITOR_HEARTBEAT_TTL_SECONDS


class TaskQueueMonitorStatus(StrEnum):
    Active = "active"
    Cancelled = "cancelled"
    Complete = "complete"
    TimedOut = "timed-out"


class TaskQueueMonitorRequest(ContractModel):
    workspace_name: str
    stub_id: str
    container_id: str
    task_id: str
    task_status: TaskStatus = TaskStatus.Running
    claimed: bool = True
    cancellation_requested: bool = False
    timeout_elapsed: bool = False


class TaskQueueMonitorPlan(ContractModel):
    status: TaskQueueMonitorStatus
    ok: bool = True
    cancelled: bool = False
    complete: bool = False
    timed_out: bool = False
    complete_dispatcher: bool = False
    next_status: TaskStatus | None = None
    heartbeat_key: str
    heartbeat_ttl_seconds: int = DEFAULT_TASK_QUEUE_MONITOR_HEARTBEAT_TTL_SECONDS
    running_lock_key: str
    running_lock_ttl_seconds: int = DEFAULT_TASK_QUEUE_MONITOR_RUNNING_LOCK_TTL_SECONDS
    poll_interval_seconds: int = DEFAULT_TASK_QUEUE_MONITOR_POLL_INTERVAL_SECONDS


class TaskQueueCompletionAction(StrEnum):
    Complete = "complete"
    Retry = "retry"


class TaskQueueCompleteRequest(ContractModel):
    workspace_name: str
    stub_id: str
    container_id: str
    task_id: str
    task_status: TaskStatus
    task_duration_ms: float = Field(default=0.0, ge=0)
    keep_warm_seconds: int = Field(default=0, ge=0)
    result: bytes | None = None
    error: str = ""
    retry_count: int = Field(default=0, ge=0)
    retry_limit: int = Field(default=0, ge=0)
    retry_delay_seconds: float = Field(default=0.0, ge=0)


class TaskQueueCompletePlan(ContractModel):
    task_status: TaskStatus
    final_status: TaskStatus
    action: TaskQueueCompletionAction
    terminal: bool
    keep_warm_lock_key: str | None = None
    running_lock_index_key: str
    running_lock_key: str
    task_duration_key: str
    task_duration_ms: float
    store_result: bool = False
    result_size_bytes: int = 0
    retry_limit_exceeded: bool = False
    retry_message: str = ""
    error: str = ""
    retry_delay_seconds: float = 0.0

    @computed_field
    @property
    def retry(self) -> bool:
        return self.action is TaskQueueCompletionAction.Retry


class TaskQueueTaskCancellationReason(StrEnum):
    Expired = "expired"
    ExceededRetryLimit = "exceeded_retry_limit"
    RequestCancelled = "request_cancelled"
    InvalidRequestPayload = "invalid_request_payload"


class TaskQueueTaskCancellationDecision(ContractModel):
    current_status: TaskStatus
    reason: TaskQueueTaskCancellationReason
    next_status: TaskStatus
    should_update: bool
    terminal_before_cancel: bool = False


def task_queue_list_key(workspace_name: str, stub_id: str) -> str:
    return f"taskqueue:{workspace_name}:{stub_id}"


def task_queue_instance_lock_key(workspace_name: str, stub_id: str) -> str:
    return f"taskqueue:{workspace_name}:{stub_id}:instance_lock"


def task_queue_task_heartbeat_key(workspace_name: str, stub_id: str, task_id: str) -> str:
    return f"taskqueue:{workspace_name}:{stub_id}:task:heartbeat:{task_id}"


def task_queue_task_duration_key(workspace_name: str, stub_id: str) -> str:
    return f"taskqueue:{workspace_name}:{stub_id}:task_duration"


def task_queue_average_task_duration_key(workspace_name: str, stub_id: str) -> str:
    return f"taskqueue:{workspace_name}:{stub_id}:avg_task_duration"


def task_queue_scheduler_serve_lock_key(workspace_name: str, stub_id: str) -> str:
    return f"scheduler:serve:lock:{workspace_name}:{stub_id}"


def decide_task_queue_serve_scale(
    sample: TaskQueueAutoscalerSample,
    *,
    serve_lock_present: bool | None,
) -> TaskQueueScaleDecision:
    if serve_lock_present is None:
        return TaskQueueScaleDecision(
            decision=TaskQueueScaleDecisionKind.Invalid,
            reason=TaskQueueScaleReason.ServeLockUnknown,
            valid=False,
            sample=sample,
        )
    desired = 1 if serve_lock_present else 0
    return TaskQueueScaleDecision(
        decision=_scale_kind(desired, sample.current_containers),
        reason=(
            TaskQueueScaleReason.ServeLockPresent
            if serve_lock_present
            else TaskQueueScaleReason.ServeLockMissing
        ),
        desired_containers=desired,
        sample=sample,
    )


def plan_task_queue_serve(request: TaskQueueServeRequest) -> TaskQueueServePlan:
    return TaskQueueServePlan(
        request=request,
        entrypoint=[request.python_executable, "-m", request.runner_module],
        serve_lock_key=task_queue_scheduler_serve_lock_key(request.workspace_name, request.stub_id),
        serve_lock_ttl_seconds=request.timeout_seconds,
        wait_timeout_seconds=request.timeout_seconds,
    )


def plan_task_queue_serve_completion(
    request: TaskQueueServeRequest,
    *,
    container_id: str,
) -> TaskQueueServeCompletionPlan:
    return TaskQueueServeCompletionPlan(
        workspace_name=request.workspace_name,
        stub_id=request.stub_id,
        container_id=container_id,
        release_keep_warm_lock_key=task_queue_keep_warm_lock_key(
            request.workspace_name,
            request.stub_id,
            container_id,
        ),
    )


def plan_task_queue_put(request: TaskQueuePutRequest) -> TaskQueuePutPlan:
    if request.tasks_in_flight >= request.max_pending_tasks:
        return TaskQueuePutPlan(
            status=TaskQueuePutStatus.TooManyPendingTasks,
            accepted=False,
            reason="max pending tasks exceeded",
        )
    payload: HttpTaskPayload | None = None
    if request.parse_payload:
        try:
            payload = serialize_http_task_payload(request.body, query_params=request.query_params)
        except ValueError as exc:
            return TaskQueuePutPlan(
                status=TaskQueuePutStatus.InvalidPayload,
                accepted=False,
                reason=str(exc),
            )
    return TaskQueuePutPlan(
        status=TaskQueuePutStatus.Accepted,
        accepted=True,
        payload=payload,
        ttl_seconds=request.ttl_seconds or DEFAULT_TASK_QUEUE_TASK_TTL_SECONDS,
    )


def plan_task_queue_pop(request: TaskQueuePopRequest) -> TaskQueuePopPlan:
    queue_key = task_queue_list_key(request.workspace_name, request.stub_id)
    processing_lock_key = task_queue_processing_lock_key(
        request.workspace_name,
        request.stub_id,
        request.container_id,
    )
    if request.queue_length == 0 or request.task_message is None:
        return TaskQueuePopPlan(
            status=TaskQueuePopStatus.Empty,
            queue_key=queue_key,
            processing_lock_key=processing_lock_key,
        )

    running_lock_index_key = task_queue_running_lock_index_key(
        request.workspace_name,
        request.stub_id,
        request.container_id,
    )
    running_lock_key = task_queue_running_lock_key(
        request.workspace_name,
        request.stub_id,
        request.container_id,
        request.task_message.task_id,
    )
    heartbeat_key = task_queue_task_heartbeat_key(
        request.workspace_name,
        request.stub_id,
        request.task_message.task_id,
    )
    if request.task_status is not None and is_terminal_task_status(request.task_status):
        return TaskQueuePopPlan(
            status=TaskQueuePopStatus.SkipCompleted,
            queue_key=queue_key,
            processing_lock_key=processing_lock_key,
            task_message=request.task_message,
            cleanup_completed_task=True,
            running_lock_index_key=running_lock_index_key,
            running_lock_key=running_lock_key,
            heartbeat_key=heartbeat_key,
        )
    return TaskQueuePopPlan(
        status=TaskQueuePopStatus.Claim,
        queue_key=queue_key,
        processing_lock_key=processing_lock_key,
        task_message=request.task_message,
        claim_task=True,
        running_lock_index_key=running_lock_index_key,
        running_lock_key=running_lock_key,
        heartbeat_key=heartbeat_key,
    )


def plan_task_queue_monitor(request: TaskQueueMonitorRequest) -> TaskQueueMonitorPlan:
    heartbeat_key = task_queue_task_heartbeat_key(
        request.workspace_name,
        request.stub_id,
        request.task_id,
    )
    running_lock_key = task_queue_running_lock_key(
        request.workspace_name,
        request.stub_id,
        request.container_id,
        request.task_id,
    )
    if request.cancellation_requested or request.task_status is TaskStatus.Cancelled:
        return TaskQueueMonitorPlan(
            status=TaskQueueMonitorStatus.Cancelled,
            cancelled=True,
            complete_dispatcher=True,
            next_status=TaskStatus.Cancelled,
            heartbeat_key=heartbeat_key,
            running_lock_key=running_lock_key,
        )
    if request.timeout_elapsed or request.task_status is TaskStatus.Timeout:
        return TaskQueueMonitorPlan(
            status=TaskQueueMonitorStatus.TimedOut,
            timed_out=True,
            complete_dispatcher=True,
            next_status=TaskStatus.Timeout,
            heartbeat_key=heartbeat_key,
            running_lock_key=running_lock_key,
        )
    if not request.claimed or is_terminal_task_status(request.task_status):
        return TaskQueueMonitorPlan(
            status=TaskQueueMonitorStatus.Complete,
            complete=True,
            heartbeat_key=heartbeat_key,
            running_lock_key=running_lock_key,
        )
    return TaskQueueMonitorPlan(
        status=TaskQueueMonitorStatus.Active,
        heartbeat_key=heartbeat_key,
        running_lock_key=running_lock_key,
    )


def plan_task_queue_complete(request: TaskQueueCompleteRequest) -> TaskQueueCompletePlan:
    action = (
        TaskQueueCompletionAction.Retry
        if request.task_status is TaskStatus.Retry
        else TaskQueueCompletionAction.Complete
    )
    retry_limit_exceeded = (
        action is TaskQueueCompletionAction.Retry and request.retry_count >= request.retry_limit
    )
    final_status = (
        TaskStatus.Failed
        if retry_limit_exceeded
        else TaskStatus.Retry
        if action is TaskQueueCompletionAction.Retry
        else request.task_status
    )
    return TaskQueueCompletePlan(
        task_status=request.task_status,
        final_status=final_status,
        action=action,
        terminal=is_terminal_task_status(final_status),
        keep_warm_lock_key=(
            task_queue_keep_warm_lock_key(
                request.workspace_name,
                request.stub_id,
                request.container_id,
            )
            if request.keep_warm_seconds > 0
            else None
        ),
        running_lock_index_key=task_queue_running_lock_index_key(
            request.workspace_name,
            request.stub_id,
            request.container_id,
        ),
        running_lock_key=task_queue_running_lock_key(
            request.workspace_name,
            request.stub_id,
            request.container_id,
            request.task_id,
        ),
        task_duration_key=task_queue_task_duration_key(request.workspace_name, request.stub_id),
        task_duration_ms=request.task_duration_ms,
        store_result=request.result is not None,
        result_size_bytes=len(request.result) if request.result is not None else 0,
        retry_limit_exceeded=retry_limit_exceeded,
        retry_message=(
            f"Exceeded retry limit of {request.retry_limit} for task <{request.task_id}>"
            if retry_limit_exceeded
            else ""
        ),
        error=request.error,
        retry_delay_seconds=request.retry_delay_seconds,
    )


def task_queue_status_for_cancellation(reason: TaskQueueTaskCancellationReason) -> TaskStatus:
    if reason is TaskQueueTaskCancellationReason.Expired:
        return TaskStatus.Expired
    return TaskStatus.Failed


def task_queue_cancellation_decision(
    current_status: TaskStatus,
    reason: TaskQueueTaskCancellationReason,
) -> TaskQueueTaskCancellationDecision:
    terminal = is_terminal_task_status(current_status)
    return TaskQueueTaskCancellationDecision(
        current_status=current_status,
        reason=reason,
        next_status=(current_status if terminal else task_queue_status_for_cancellation(reason)),
        should_update=not terminal,
        terminal_before_cancel=terminal,
    )


def _scale_kind(desired: int, current: int) -> TaskQueueScaleDecisionKind:
    if desired > current:
        return TaskQueueScaleDecisionKind.ScaleUp
    if desired < current:
        return TaskQueueScaleDecisionKind.ScaleDown
    return TaskQueueScaleDecisionKind.Hold
