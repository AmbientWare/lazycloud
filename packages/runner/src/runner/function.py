from __future__ import annotations

import contextlib
import os
import socket
import sys
import time
import traceback
from dataclasses import dataclass, field
from typing import Any, Protocol, TextIO

import cloudpickle
from foundation.handler_loading import load_callable
from pydantic import BaseModel, ConfigDict, Field, JsonValue, ValidationError
from shared.deployments import DeploymentKind
from shared.env import (
    APP_ID_ENV,
    CONTAINER_ID_ENV,
    GATEWAY_HTTP_URL_ENV,
    GATEWAY_TOKEN_ENV,
    KEEP_WARM_SECONDS_ENV,
    LIFECYCLE_HOOKS_ENV,
    ROOT_TASK_ID_ENV,
    TASK_ID_ENV,
    WORKSPACE_ID_ENV,
    WORKSPACE_NAME_ENV,
)
from shared.function_payloads import (
    FUNCTION_MARKER_MAX_DEPTH,
    FUNCTION_MARKER_MAX_NODES,
    FunctionCloudpickleResult,
    FunctionDependencyBinding,
    FunctionJsonInvocation,
    FunctionJsonResult,
    FunctionPayloadEncoding,
    FunctionResultPayload,
)
from shared.http.errors import HttpApiError
from shared.http.functions import (
    FUNCTION_CALL_REF_MARKER,
    FunctionClaimedTask,
    FunctionClaimRequest,
    FunctionClaimResponse,
    FunctionSetResultBody,
    FunctionSetResultResponse,
)
from shared.http.gateway_tasks import (
    AppendTaskLogRequest,
    AppendTaskLogResponse,
    EndTaskRequest,
    EndTaskResponse,
    StartTaskRequest,
    StartTaskResponse,
)
from shared.http_transport import HttpChannel
from shared.lifecycle import (
    LifecycleHookName,
    LifecycleHooks,
    LifecycleStartupContext,
    LifecycleTaskContext,
)
from shared.serialization import to_json_value
from shared.tasks import TaskStatus

from runner.hooks import lifecycle_hooks_from_env, run_lifecycle_hooks
from runner.invocation import cloudpickle_bytes, invoke_handler
from runner.runtime import (
    DEFAULT_GATEWAY_ENDPOINT,
    DEFAULT_RUNNER_TIMEOUT_SECONDS,
    RunnerTaskLogStream,
    required_env,
)

# How often an idle container asks for work. Short enough that a call arriving
# at a warm container is served promptly, which is the whole point of holding
# one open.
DEFAULT_FUNCTION_POLL_INTERVAL_SECONDS = 0.1


@dataclass(frozen=True, slots=True)
class FunctionRunnerConfig:
    """What a function container is, independent of any call it serves.

    There is no task here. The container is started for the stub and finds out
    which invocation it is running by claiming one, so everything that varies per
    call lives in `ClaimedTask` instead.
    """

    stub_id: str
    handler_ref: str
    endpoint: str = DEFAULT_GATEWAY_ENDPOINT
    token: str = ""
    container_id: str = ""
    container_hostname: str = ""
    workspace_id: str = ""
    workspace_name: str = ""
    app_id: str = ""
    lifecycle_hooks: LifecycleHooks = field(default_factory=LifecycleHooks)
    timeout_seconds: float = DEFAULT_RUNNER_TIMEOUT_SECONDS
    keep_warm_seconds: int = 0
    poll_interval_seconds: float = DEFAULT_FUNCTION_POLL_INTERVAL_SECONDS


@dataclass(frozen=True, slots=True)
class ClaimedTask:
    """One invocation this container has taken ownership of."""

    task_id: str
    root_task_id: str
    attempt_number: int
    max_attempts: int
    invocation: FunctionInvocation


class FunctionInvocation(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True, extra="forbid")

    args: tuple[Any, ...] = ()
    kwargs: dict[str, Any] = Field(default_factory=dict)
    result_format: FunctionPayloadEncoding = FunctionPayloadEncoding.Cloudpickle


class FunctionControlChannel(Protocol):
    def post(
        self,
        path: str,
        payload: dict[str, JsonValue] | None = None,
    ) -> JsonValue: ...


class FunctionTaskLogSink(Protocol):
    def append_task_log(self, task_id: str, stream: str, message: str) -> None: ...


@dataclass(slots=True)
class FunctionRunner:
    config: FunctionRunnerConfig
    channel: FunctionControlChannel | None = None
    _handler: Any = field(default=None, init=False)
    _startup_hooks_ran: bool = field(default=False, init=False)

    @property
    def control(self) -> FunctionControlChannel:
        if self.channel is None:
            self.channel = HttpChannel(
                endpoint=self.config.endpoint,
                token=self.config.token or None,
                timeout_seconds=self.config.timeout_seconds,
            )
        return self.channel

    def run(self) -> int:
        """Serve invocations for this stub until the container goes idle.

        A failing invocation is reported and the loop continues — it is one
        caller's error, not this container's. Startup failing is the opposite: a
        container whose `on_start` did not finish cannot serve anything, so it
        stops rather than answering claims it will fail.
        """

        try:
            self.run_startup_hooks_once()
        except BaseException:
            print(traceback.format_exc(), file=sys.stderr)
            return 1
        idle_since = time.monotonic()
        while True:
            task = self.claim()
            if task is None:
                if self.keep_warm_expired(idle_since):
                    return 0
                time.sleep(self.config.poll_interval_seconds)
                continue
            self.run_task(task)
            idle_since = time.monotonic()

    def keep_warm_expired(self, idle_since: float) -> bool:
        if self.config.keep_warm_seconds < 0:
            return False
        return time.monotonic() - idle_since >= self.config.keep_warm_seconds

    def claim(self) -> ClaimedTask | None:
        try:
            response = FunctionClaimResponse.model_validate(
                self.control.post(
                    "/api/v1/functions/claim",
                    FunctionClaimRequest(
                        stub_id=self.config.stub_id,
                        container_id=self.config.container_id,
                    ).model_dump(mode="json"),
                )
            )
        except Exception as exc:
            # A control plane that cannot be reached is not an empty queue. Say so
            # on the container's own stream — there is no task to attribute it to.
            print(f"function claim failed: {exc}", file=sys.stderr, flush=True)
            time.sleep(self.config.poll_interval_seconds)
            return None
        if response.task is None:
            return None
        claimed = response.task
        return ClaimedTask(
            task_id=claimed.task_id,
            root_task_id=claimed.root_task_id or claimed.task_id,
            attempt_number=claimed.attempt_number,
            max_attempts=claimed.max_attempts,
            invocation=decode_function_invocation(claimed),
        )

    def run_task(self, task: ClaimedTask) -> None:
        started = time.perf_counter()
        # The SDK reads its own task identity from the environment, so a pooled
        # container has to restate it per call rather than inherit it at start.
        # One task runs at a time in this process, which is what makes a process
        # global safe to use here.
        os.environ[TASK_ID_ENV] = task.task_id
        os.environ[ROOT_TASK_ID_ENV] = task.root_task_id
        try:
            self.start_task(task)
            self.run_task_hooks(task, LifecycleHookName.Running, TaskStatus.Running)
            result = self.execute_with_log_capture(task)
            self.set_result(task, _serialize_function_result(result, task.invocation))
            duration = time.perf_counter() - started
            self.run_task_hooks(
                task,
                LifecycleHookName.Success,
                TaskStatus.Complete,
                duration_seconds=duration,
                result_available=True,
            )
            self.run_task_hooks(
                task,
                LifecycleHookName.Finish,
                TaskStatus.Complete,
                duration_seconds=duration,
                result_available=True,
            )
        except BaseException as exc:
            duration = time.perf_counter() - started
            formatted = traceback.format_exc()
            with contextlib.suppress(Exception):
                self.append_task_log(task.task_id, "stderr", formatted)
            print(formatted, file=sys.stderr)
            self.run_error_hooks(task, exc, duration_seconds=duration)
            response = self.end_failed_task(task, exc, duration_seconds=duration)
            self.run_final_failure_hooks(task, exc, response, duration_seconds=duration)

    def handler(self) -> Any:
        if self._handler is None:
            self._handler = load_callable(self.config.handler_ref)
        return self._handler

    def execute_with_log_capture(self, task: ClaimedTask) -> Any:
        stdout = TaskLogStream(self, task.task_id, "stdout", sys.stdout)
        stderr = TaskLogStream(self, task.task_id, "stderr", sys.stderr)
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            try:
                return invoke_handler(
                    self.handler(),
                    *task.invocation.args,
                    **task.invocation.kwargs,
                )
            finally:
                stdout.flush_log()
                stderr.flush_log()

    def start_task(self, task: ClaimedTask) -> None:
        StartTaskResponse.model_validate(
            self.control.post(
                "/gateway/tasks/start",
                StartTaskRequest(
                    task_id=task.task_id,
                    container_id=self.config.container_id,
                ).model_dump(mode="json"),
            )
        )

    def set_result(self, task: ClaimedTask, result: FunctionResultPayload) -> None:
        FunctionSetResultResponse.model_validate(
            self.control.post(
                "/api/v1/functions/set-result",
                FunctionSetResultBody(
                    task_id=task.task_id,
                    container_id=self.config.container_id,
                    result=result,
                ).model_dump(mode="json"),
            )
        )

    def append_task_log(self, task_id: str, stream: str, message: str) -> None:
        if not message:
            return
        AppendTaskLogResponse.model_validate(
            self.control.post(
                "/gateway/tasks/log",
                AppendTaskLogRequest(
                    task_id=task_id,
                    stream=stream,
                    message=message,
                ).model_dump(mode="json"),
            )
        )

    def append_container_log(self, stream: str, message: str) -> None:
        """Write to the container's own stream, for output no task owns."""

        if stream == "stderr":
            print(message, file=sys.stderr, end="", flush=True)
        else:
            print(message, end="", flush=True)

    def end_failed_task(
        self,
        task: ClaimedTask,
        exc: BaseException,
        *,
        duration_seconds: float,
    ) -> EndTaskResponse | None:
        try:
            return EndTaskResponse.model_validate(
                self.control.post(
                    "/gateway/tasks/end",
                    EndTaskRequest(
                        task_id=task.task_id,
                        task_duration=duration_seconds,
                        task_status=TaskStatus.Failed,
                        container_id=self.config.container_id,
                        container_hostname=self.config.container_hostname,
                        result_base64="",
                    ).model_dump(mode="json"),
                )
            )
        except HttpApiError as end_exc:
            print(
                end_exc.detail or f"failed to mark task failed after {type(exc).__name__}",
                file=sys.stderr,
            )
            return None

    def run_startup_hooks_once(self) -> None:
        """Prepare this container to serve, exactly once.

        Importing the handler happens here rather than at the first invocation,
        so the import cost is paid by the container's startup instead of by
        whichever call happened to arrive first. `on_start` runs after it, which
        is the ordering the hook was always documented to have and never had
        while a process served exactly one call.

        Startup output goes to the container's stream, not to a task's log: the
        first task to arrive did not cause this work and must not be the record
        of it.
        """

        if self._startup_hooks_ran:
            return
        self._startup_hooks_ran = True
        self.handler()
        context = LifecycleStartupContext(
            stub_id=self.config.stub_id,
            workspace_id=self.config.workspace_id,
            workspace_name=self.config.workspace_name,
            app_id=self.config.app_id,
            container_id=self.config.container_id,
            container_hostname=self.config.container_hostname,
            handler=self.config.handler_ref,
            resource_kind=DeploymentKind.Function,
        )
        run_lifecycle_hooks(
            self.config.lifecycle_hooks,
            LifecycleHookName.Start,
            context,
            log=self.append_container_log,
            capture_output=False,
        )

    def run_error_hooks(
        self,
        task: ClaimedTask,
        exc: BaseException,
        *,
        duration_seconds: float,
    ) -> None:
        self.run_task_hooks(
            task,
            LifecycleHookName.Error,
            TaskStatus.Failed,
            duration_seconds=duration_seconds,
            error_type=type(exc).__name__,
            error_message=str(exc),
        )

    def run_final_failure_hooks(
        self,
        task: ClaimedTask,
        exc: BaseException,
        response: EndTaskResponse | None,
        *,
        duration_seconds: float,
    ) -> None:
        end_status = response.final_status if response is not None else None
        final_status = end_status or TaskStatus.Failed
        retry_scheduled = response.retry_scheduled if response is not None else False
        attempt_number = response.attempt_number if response is not None else 0
        max_attempts = response.max_attempts if response is not None else 0
        hook = LifecycleHookName.Retry if retry_scheduled else LifecycleHookName.Failure
        self.run_task_hooks(
            task,
            hook,
            final_status,
            duration_seconds=duration_seconds,
            error_type=type(exc).__name__,
            error_message=str(exc),
            retry_scheduled=retry_scheduled,
            attempt_number=attempt_number,
            max_attempts=max_attempts,
        )
        self.run_task_hooks(
            task,
            LifecycleHookName.Finish,
            final_status,
            duration_seconds=duration_seconds,
            error_type=type(exc).__name__,
            error_message=str(exc),
            retry_scheduled=retry_scheduled,
            attempt_number=attempt_number,
            max_attempts=max_attempts,
        )

    def run_task_hooks(
        self,
        task: ClaimedTask,
        hook: LifecycleHookName,
        status: TaskStatus,
        *,
        duration_seconds: float = 0.0,
        error_type: str = "",
        error_message: str = "",
        retry_scheduled: bool = False,
        result_available: bool = False,
        attempt_number: int = 0,
        max_attempts: int = 1,
    ) -> None:
        context = LifecycleTaskContext(
            hook=hook,
            task_id=task.task_id,
            status=status,
            stub_id=self.config.stub_id,
            root_task_id=task.root_task_id,
            workspace_id=self.config.workspace_id,
            workspace_name=self.config.workspace_name,
            app_id=self.config.app_id,
            container_id=self.config.container_id,
            container_hostname=self.config.container_hostname,
            handler=self.config.handler_ref,
            resource_kind=DeploymentKind.Function,
            attempt_number=attempt_number or task.attempt_number,
            max_attempts=max_attempts or task.max_attempts,
            duration_seconds=duration_seconds,
            error_type=error_type,
            error_message=error_message,
            retry_scheduled=retry_scheduled,
            result_available=result_available,
        )
        run_lifecycle_hooks(
            self.config.lifecycle_hooks,
            hook,
            context,
            log=lambda stream, message: self.append_task_log(task.task_id, stream, message),
        )


class TaskLogStream(RunnerTaskLogStream):
    def __init__(
        self,
        runner: FunctionTaskLogSink,
        task_id: str,
        stream: str,
        wrapped: TextIO,
    ) -> None:
        super().__init__(stream, wrapped)
        self.runner = runner
        self.task_id = task_id

    def append_log(self, value: str) -> None:
        self.runner.append_task_log(self.task_id, self.stream, value)


def decode_function_invocation(response: FunctionClaimedTask) -> FunctionInvocation:
    if isinstance(response.invocation, FunctionJsonInvocation):
        invocation = FunctionInvocation(
            args=tuple(response.invocation.args),
            kwargs=response.invocation.kwargs,
            result_format=response.invocation.result_encoding,
        )
    else:
        payload = cloudpickle.loads(response.invocation.bytes_value())
        try:
            invocation = FunctionInvocation.model_validate(payload)
        except ValidationError as exc:
            raise ValueError("invalid function invocation envelope") from exc
    values = {
        binding.task_id: _decode_dependency_result(binding) for binding in response.dependencies
    }
    used: set[str] = set()
    state = _MarkerTraversalState()
    invocation.args = tuple(
        _replace_dependency_markers(item, values, used, state, depth=0) for item in invocation.args
    )
    invocation.kwargs = {
        key: _replace_dependency_markers(item, values, used, state, depth=0)
        for key, item in invocation.kwargs.items()
    }
    if used != set(values):
        unused = sorted(set(values) - used)
        raise ValueError(f"unused function dependency bindings: {', '.join(unused)}")
    return invocation


@dataclass(slots=True)
class _MarkerTraversalState:
    nodes: int = 0
    memo: dict[int, Any] = field(default_factory=dict)
    active_tuples: set[int] = field(default_factory=set)


def _replace_dependency_markers(
    value: Any,
    values: dict[str, Any],
    used: set[str],
    state: _MarkerTraversalState,
    *,
    depth: int,
) -> Any:
    if depth > FUNCTION_MARKER_MAX_DEPTH:
        raise ValueError(f"function dependency marker depth exceeds {FUNCTION_MARKER_MAX_DEPTH}")
    state.nodes += 1
    if state.nodes > FUNCTION_MARKER_MAX_NODES:
        raise ValueError(f"function dependency marker nodes exceed {FUNCTION_MARKER_MAX_NODES}")
    if isinstance(value, dict):
        return _replace_dependency_mapping(value, values, used, state, depth=depth)
    if isinstance(value, list):
        return _replace_dependency_list(value, values, used, state, depth=depth)
    if isinstance(value, tuple):
        return _replace_dependency_tuple(value, values, used, state, depth=depth)
    return value


def _replace_dependency_mapping(
    value: Any,
    values: dict[str, Any],
    used: set[str],
    state: _MarkerTraversalState,
    *,
    depth: int,
) -> Any:
    if (
        len(value) == 2
        and value.get(FUNCTION_CALL_REF_MARKER) is True
        and isinstance(value.get("task_id"), str)
    ):
        task_id = value["task_id"]
        if task_id not in values:
            raise ValueError(f"undeclared function dependency marker: {task_id}")
        used.add(task_id)
        return values[task_id]
    identity = id(value)
    if identity in state.memo:
        return state.memo[identity]
    replaced: dict[Any, Any] = {}
    state.memo[identity] = replaced
    for key, item in value.items():
        replaced[key] = _replace_dependency_markers(
            item,
            values,
            used,
            state,
            depth=depth + 1,
        )
    return replaced


def _replace_dependency_list(
    value: Any,
    values: dict[str, Any],
    used: set[str],
    state: _MarkerTraversalState,
    *,
    depth: int,
) -> Any:
    identity = id(value)
    if identity in state.memo:
        return state.memo[identity]
    replaced: list[Any] = []
    state.memo[identity] = replaced
    replaced.extend(
        _replace_dependency_markers(item, values, used, state, depth=depth + 1) for item in value
    )
    return replaced


def _replace_dependency_tuple(
    value: Any,
    values: dict[str, Any],
    used: set[str],
    state: _MarkerTraversalState,
    *,
    depth: int,
) -> Any:
    identity = id(value)
    if identity in state.active_tuples:
        raise ValueError("cyclic tuple in function invocation is unsupported")
    if identity in state.memo:
        return state.memo[identity]
    state.active_tuples.add(identity)
    try:
        replaced = tuple(
            _replace_dependency_markers(item, values, used, state, depth=depth + 1)
            for item in value
        )
    finally:
        state.active_tuples.remove(identity)
    state.memo[identity] = replaced
    return replaced


def _decode_dependency_result(binding: FunctionDependencyBinding) -> Any:
    if binding.result.encoding is FunctionPayloadEncoding.Json:
        return binding.result.value
    return cloudpickle.loads(binding.result.bytes_value())


def execute_handler(handler_ref: str, invocation: FunctionInvocation) -> Any:
    handler = load_callable(handler_ref)
    return invoke_handler(handler, *invocation.args, **invocation.kwargs)


def config_from_env(env: dict[str, str] | None = None) -> FunctionRunnerConfig:
    source = env or os.environ
    return FunctionRunnerConfig(
        stub_id=required_env(source, "STUB_ID"),
        handler_ref=required_env(source, "HANDLER"),
        endpoint=source.get(GATEWAY_HTTP_URL_ENV) or DEFAULT_GATEWAY_ENDPOINT,
        token=source.get(GATEWAY_TOKEN_ENV, ""),
        container_id=source.get(CONTAINER_ID_ENV) or socket.gethostname(),
        container_hostname=socket.gethostname(),
        workspace_id=source.get(WORKSPACE_ID_ENV, ""),
        workspace_name=source.get(WORKSPACE_NAME_ENV, ""),
        app_id=source.get(APP_ID_ENV, ""),
        lifecycle_hooks=lifecycle_hooks_from_env(source.get(LIFECYCLE_HOOKS_ENV)),
        keep_warm_seconds=_keep_warm_seconds(source.get(KEEP_WARM_SECONDS_ENV)),
        poll_interval_seconds=DEFAULT_FUNCTION_POLL_INTERVAL_SECONDS,
    )


def _keep_warm_seconds(value: str | None) -> int:
    if not value:
        return 0
    try:
        return int(value)
    except ValueError:
        return 0


def main() -> int:
    return FunctionRunner(config_from_env()).run()


def _serialize_function_result(
    result: Any,
    invocation: FunctionInvocation,
) -> FunctionResultPayload:
    if invocation.result_format is FunctionPayloadEncoding.Json:
        return FunctionJsonResult(value=to_json_value(result))
    return FunctionCloudpickleResult.from_bytes(cloudpickle_bytes(result))


if __name__ == "__main__":
    raise SystemExit(main())
