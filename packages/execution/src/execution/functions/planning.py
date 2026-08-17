from __future__ import annotations

from enum import StrEnum

from compute.resources import normalize_gpu_count
from pydantic import Field, computed_field
from shared.contracts import ContractModel
from shared.deployments import DeploymentKind
from shared.env import (
    APP_ID_ENV,
    CHECKPOINT_ENABLED_ENV,
    CONTAINER_ID_ENV,
    FUNCTION_CONCURRENCY_ENV,
    FUNCTION_IN_PROCESS_ENV,
    GATEWAY_TOKEN_ENV,
    KEEP_WARM_SECONDS_ENV,
    LIFECYCLE_HOOKS_ENV,
    WORKSPACE_ID_ENV,
    WORKSPACE_NAME_ENV,
)
from shared.function_payloads import FunctionInvocationPayload
from shared.lifecycle import LifecycleHooks
from shared.tasks import (
    TaskStatus,
    is_inflight_task_status,
    is_terminal_task_status,
)

from execution.config import ManagedPythonExecutable

DEFAULT_FUNCTION_CONTAINER_CPU_MILLICORES = 100
DEFAULT_FUNCTION_CONTAINER_MEMORY_MIB = 128
DEFAULT_FUNCTION_HEARTBEAT_TIMEOUT_SECONDS = 60
DEFAULT_FUNCTION_MONITOR_POLL_INTERVAL_SECONDS = 1
FUNCTION_RUNNER_MODULE = "runner.function"
FUNCTION_DEFAULT_PYTHON_EXECUTABLE = "python3.12"


class FunctionInvokeRequest(ContractModel):
    invocation: FunctionInvocationPayload
    headless: bool = False
    configured_retry_count: int = Field(default=0, ge=0)


class FunctionInvokePlan(ContractModel):
    invocation: FunctionInvocationPayload
    retry_count: int = 0
    headless: bool = False


class FunctionContainerEnvVar(StrEnum):
    Concurrency = FUNCTION_CONCURRENCY_ENV
    KeepWarmSeconds = KEEP_WARM_SECONDS_ENV
    Handler = "HANDLER"
    GatewayToken = GATEWAY_TOKEN_ENV
    StubId = "STUB_ID"
    ContainerId = CONTAINER_ID_ENV
    WorkspaceId = WORKSPACE_ID_ENV
    WorkspaceName = WORKSPACE_NAME_ENV
    AppId = APP_ID_ENV
    LifecycleHooks = LIFECYCLE_HOOKS_ENV
    CheckpointEnabled = CHECKPOINT_ENABLED_ENV
    InProcess = FUNCTION_IN_PROCESS_ENV


class FunctionContainerStartRequest(ContractModel):
    workspace_name: str
    workspace_id: str = ""
    app_id: str = ""
    stub_id: str
    handler: str
    keep_warm_seconds: int = 0
    concurrency: int = Field(default=1, gt=0)
    in_process: bool = False
    gateway_token: str = ""
    container_id: str
    python_executable: ManagedPythonExecutable = FUNCTION_DEFAULT_PYTHON_EXECUTABLE
    runner_module: str = FUNCTION_RUNNER_MODULE
    cpu_millicores: int = Field(default=0, ge=0)
    memory_mib: int = Field(default=0, ge=0)
    disk_mib: int = Field(default=0, ge=0)
    requires_gpu: bool = False
    gpu_count: int = Field(default=0, ge=0)
    gpu_request: list[str] = Field(default_factory=list)
    image_id: str = ""
    checkpoint_enabled: bool = False
    env: list[str] = Field(default_factory=list)
    secret_env: list[str] = Field(default_factory=list)
    lifecycle_hooks: LifecycleHooks = Field(default_factory=LifecycleHooks)


class FunctionContainerStartPlan(ContractModel):
    container_id: str
    entrypoint: list[str]
    env: list[str]
    cpu_millicores: int
    memory_mib: int
    disk_mib: int = 0
    gpu_count: int
    gpu_request: list[str]
    image_id: str

    @computed_field
    @property
    def requires_gpu(self) -> bool:
        return self.gpu_count > 0 or bool(self.gpu_request)


class FunctionMonitorStatus(StrEnum):
    Active = "active"
    Cancelled = "cancelled"
    Complete = "complete"
    TimedOut = "timed-out"


class FunctionMonitorRequest(ContractModel):
    workspace_id: str
    stub_id: str
    container_id: str
    task_id: str
    task_status: TaskStatus = TaskStatus.Running
    claimed: bool = True
    cancellation_requested: bool = False
    timeout_elapsed: bool = False


class FunctionMonitorPlan(ContractModel):
    status: FunctionMonitorStatus
    ok: bool = True
    cancelled: bool = False
    complete: bool = False
    timed_out: bool = False
    complete_dispatcher: bool = False
    next_status: TaskStatus | None = None
    heartbeat_key: str
    heartbeat_ttl_seconds: int = DEFAULT_FUNCTION_HEARTBEAT_TIMEOUT_SECONDS
    cancel_channel_key: str
    poll_interval_seconds: int = DEFAULT_FUNCTION_MONITOR_POLL_INTERVAL_SECONDS


class FunctionHeartbeatRequest(ContractModel):
    workspace_id: str
    task_id: str
    current_status: TaskStatus
    running_age_seconds: int = Field(default=0, ge=0)
    heartbeat_present: bool = False


class FunctionHeartbeatPlan(ContractModel):
    alive: bool
    heartbeat_key: str
    heartbeat_ttl_seconds: int = DEFAULT_FUNCTION_HEARTBEAT_TIMEOUT_SECONDS
    checked_heartbeat_key: bool


class FunctionStreamCancelRequest(ContractModel):
    workspace_id: str
    stub_id: str
    task_id: str
    headless: bool = False
    completion_observed: bool = False
    client_disconnected: bool = True


class FunctionStreamCancelPlan(ContractModel):
    should_cancel: bool
    should_complete_dispatcher: bool
    cancel_channel_key: str
    publish_payload: str
    next_status: TaskStatus | None = None


class FunctionContainerStartAuthority(StrEnum):
    """How much capacity the caller asking for a container is entitled to.

    `ColdStart` is every path that reacts to one task: an invocation arriving, a
    retry coming due, a backlog found with nothing alive to serve it. It may
    bring a stub up from nothing so a single call is not made to wait for a
    scheduler tick, and it stops there — depth past the first container is a
    judgement about a backlog, and only the autoscaler sees one whole. Six
    invocations arriving together each reading the same counts and each deciding
    to provision is what let a ceiling of six hold nine containers.
    """

    ColdStart = "cold-start"
    Autoscaler = "autoscaler"


def function_container_start_allowed(
    *,
    authority: FunctionContainerStartAuthority,
    live_containers: int,
    max_containers: int,
) -> bool:
    """Whether one more container may be started for a stub in this state.

    Both halves are answered here so that the ceiling has exactly one reading.
    `live_containers` must have been sampled under the lock the reservation
    holds; answered against a stale count this decides nothing.
    """

    if live_containers >= max_containers:
        return False
    if authority is FunctionContainerStartAuthority.Autoscaler:
        return True
    return live_containers == 0


class FunctionTaskCancellationReason(StrEnum):
    Expired = "expired"
    ExceededRetryLimit = "exceeded_retry_limit"
    RequestCancelled = "request_cancelled"
    InvalidRequestPayload = "invalid_request_payload"


class FunctionTaskCancellationDecision(ContractModel):
    current_status: TaskStatus
    reason: FunctionTaskCancellationReason
    next_status: TaskStatus
    should_update: bool
    should_stop_container: bool = False
    terminal_before_cancel: bool = False


def function_prefix_key() -> str:
    return "function"


def function_heartbeat_key(workspace_id: str, task_id: str) -> str:
    return f"function:{workspace_id}:{task_id}:heartbeat"


def function_task_cancel_key(workspace_id: str, stub_id: str, task_id: str) -> str:
    return f"task:{workspace_id}:{stub_id}:{task_id}:cancel"


def function_container_id(stub_kind: DeploymentKind, task_id: str, suffix: str) -> str:
    return f"{stub_kind.value}-{task_id}-{suffix}"


def plan_function_invoke(request: FunctionInvokeRequest) -> FunctionInvokePlan:
    return FunctionInvokePlan(
        invocation=request.invocation,
        retry_count=request.configured_retry_count,
        headless=request.headless,
    )


def plan_function_container_start(
    request: FunctionContainerStartRequest,
) -> FunctionContainerStartPlan:
    container_id = request.container_id
    lifecycle_hooks_json = request.lifecycle_hooks.model_dump_json()
    return FunctionContainerStartPlan(
        container_id=container_id,
        entrypoint=[request.python_executable, "-m", request.runner_module],
        env=[
            *request.secret_env,
            *request.env,
            # No task id. The container is started for the stub and learns which
            # invocation it is running when it claims one, so a task named here
            # would be a guess that the claim then contradicts.
            f"{FunctionContainerEnvVar.KeepWarmSeconds.value}={request.keep_warm_seconds}",
            f"{FunctionContainerEnvVar.Concurrency.value}={request.concurrency}",
            (f"{FunctionContainerEnvVar.InProcess.value}={str(request.in_process).lower()}"),
            f"{FunctionContainerEnvVar.Handler.value}={request.handler}",
            f"{FunctionContainerEnvVar.GatewayToken.value}={request.gateway_token}",
            f"{FunctionContainerEnvVar.StubId.value}={request.stub_id}",
            f"{FunctionContainerEnvVar.ContainerId.value}={container_id}",
            f"{FunctionContainerEnvVar.WorkspaceId.value}={request.workspace_id}",
            f"{FunctionContainerEnvVar.WorkspaceName.value}={request.workspace_name}",
            f"{FunctionContainerEnvVar.AppId.value}={request.app_id}",
            f"{FunctionContainerEnvVar.LifecycleHooks.value}={lifecycle_hooks_json}",
            (
                f"{FunctionContainerEnvVar.CheckpointEnabled.value}="
                f"{str(request.checkpoint_enabled).lower()}"
            ),
        ],
        cpu_millicores=request.cpu_millicores or DEFAULT_FUNCTION_CONTAINER_CPU_MILLICORES,
        memory_mib=request.memory_mib or DEFAULT_FUNCTION_CONTAINER_MEMORY_MIB,
        disk_mib=request.disk_mib,
        gpu_count=normalize_gpu_count(request.requires_gpu, request.gpu_count),
        gpu_request=request.gpu_request,
        image_id=request.image_id,
    )


def plan_function_monitor(request: FunctionMonitorRequest) -> FunctionMonitorPlan:
    heartbeat_key = function_heartbeat_key(request.workspace_id, request.task_id)
    cancel_channel_key = function_task_cancel_key(
        request.workspace_id,
        request.stub_id,
        request.task_id,
    )
    if request.cancellation_requested or request.task_status is TaskStatus.Cancelled:
        return FunctionMonitorPlan(
            status=FunctionMonitorStatus.Cancelled,
            cancelled=True,
            complete_dispatcher=True,
            next_status=TaskStatus.Cancelled,
            heartbeat_key=heartbeat_key,
            cancel_channel_key=cancel_channel_key,
        )
    if request.timeout_elapsed or request.task_status is TaskStatus.Timeout:
        return FunctionMonitorPlan(
            status=FunctionMonitorStatus.TimedOut,
            timed_out=True,
            complete_dispatcher=True,
            next_status=TaskStatus.Timeout,
            heartbeat_key=heartbeat_key,
            cancel_channel_key=cancel_channel_key,
        )
    if not request.claimed or is_terminal_task_status(request.task_status):
        return FunctionMonitorPlan(
            status=FunctionMonitorStatus.Complete,
            complete=True,
            heartbeat_key=heartbeat_key,
            cancel_channel_key=cancel_channel_key,
        )
    return FunctionMonitorPlan(
        status=FunctionMonitorStatus.Active,
        heartbeat_key=heartbeat_key,
        cancel_channel_key=cancel_channel_key,
    )


def plan_function_heartbeat(request: FunctionHeartbeatRequest) -> FunctionHeartbeatPlan:
    bypass = (
        request.current_status is TaskStatus.Running
        and request.running_age_seconds < DEFAULT_FUNCTION_HEARTBEAT_TIMEOUT_SECONDS
    )
    return FunctionHeartbeatPlan(
        alive=bypass or request.heartbeat_present,
        heartbeat_key=function_heartbeat_key(request.workspace_id, request.task_id),
        checked_heartbeat_key=not bypass,
    )


def plan_function_stream_cancel(
    request: FunctionStreamCancelRequest,
) -> FunctionStreamCancelPlan:
    should_cancel = (
        request.client_disconnected and not request.headless and not request.completion_observed
    )
    return FunctionStreamCancelPlan(
        should_cancel=should_cancel,
        should_complete_dispatcher=should_cancel,
        cancel_channel_key=function_task_cancel_key(
            request.workspace_id,
            request.stub_id,
            request.task_id,
        ),
        publish_payload=request.task_id,
        next_status=TaskStatus.Cancelled if should_cancel else None,
    )


def function_status_for_cancellation(reason: FunctionTaskCancellationReason) -> TaskStatus:
    if reason is FunctionTaskCancellationReason.Expired:
        return TaskStatus.Expired
    if reason is FunctionTaskCancellationReason.RequestCancelled:
        return TaskStatus.Cancelled
    return TaskStatus.Failed


def function_cancellation_decision(
    current_status: TaskStatus,
    reason: FunctionTaskCancellationReason,
    *,
    container_id: str = "",
) -> FunctionTaskCancellationDecision:
    inflight = is_inflight_task_status(current_status)
    return FunctionTaskCancellationDecision(
        current_status=current_status,
        reason=reason,
        next_status=(function_status_for_cancellation(reason) if inflight else current_status),
        should_update=inflight,
        should_stop_container=inflight and bool(container_id),
        terminal_before_cancel=is_terminal_task_status(current_status),
    )
