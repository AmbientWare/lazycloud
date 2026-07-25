from __future__ import annotations

import contextlib
import json
import os
import signal
import socket
import sys
import threading
import time
import traceback
from collections.abc import Callable
from dataclasses import dataclass, field
from multiprocessing import Process
from types import FrameType
from typing import Any, Protocol, TextIO

import cloudpickle
from foundation.handler_loading import evict_user_code_modules, load_callable
from pydantic import JsonValue, TypeAdapter, ValidationError
from shared.bytes_transport import encode_bytes
from shared.deployments import DeploymentKind
from shared.env import (
    APP_ID_ENV,
    CHECKPOINT_ENABLED_ENV,
    CONTAINER_HOSTNAME_ENV,
    CONTAINER_ID_ENV,
    GATEWAY_HTTP_URL_ENV,
    GATEWAY_TOKEN_ENV,
    LIFECYCLE_HOOKS_ENV,
    STUB_ID_ENV,
    TASK_QUEUE_RETRY_FOR_ENV,
    TASK_QUEUE_WORKERS_ENV,
    WORKSPACE_ID_ENV,
    WORKSPACE_NAME_ENV,
    truthy_env_value,
)
from shared.http.gateway_tasks import AppendTaskLogRequest, AppendTaskLogResponse
from shared.http.taskqueues import (
    TaskQueueCompleteBody,
    TaskQueueCompleteResponse,
    TaskQueueInvocationEnvelope,
    TaskQueueMonitorRequest,
    TaskQueueMonitorResponse,
    TaskQueuePopRequest,
    TaskQueuePopResponse,
    TaskQueueSerializedInvocation,
    TaskQueueTaskMessage,
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

from runner.checkpoints import wait_for_checkpoint
from runner.hooks import lifecycle_hooks_from_env, run_lifecycle_hooks
from runner.invocation import invoke_handler
from runner.reload import SourceChangeWatcher, hot_reload_enabled, hot_reload_root
from runner.runtime import (
    DEFAULT_GATEWAY_ENDPOINT,
    DEFAULT_RUNNER_TIMEOUT_SECONDS,
    RunnerTaskLogStream,
    required_env,
)

DEFAULT_TASK_QUEUE_POLL_INTERVAL_SECONDS = 0.1
DEFAULT_TASK_QUEUE_CONTROL_ERROR_BACKOFF_SECONDS = 1.0
TASK_QUEUE_HANDLER_ENV = "HANDLER"
TASK_QUEUE_STUB_ID_ENV = STUB_ID_ENV
TASK_QUEUE_CONTROL_ERROR_BACKOFF_ENV = "TASK_QUEUE_CONTROL_ERROR_BACKOFF_SECONDS"
_JSON_VALUE_ADAPTER = TypeAdapter[JsonValue](JsonValue)


@dataclass(frozen=True, slots=True)
class TaskQueueInvocation:
    task_id: str
    args: tuple[Any, ...] = ()
    kwargs: dict[str, Any] = field(default_factory=dict)
    message: TaskQueueTaskMessage | None = None


@dataclass(frozen=True, slots=True)
class TaskQueueExecutionResult:
    invocation: TaskQueueInvocation
    status: TaskStatus
    response: TaskQueueCompleteResponse
    duration_seconds: float
    error: str = ""


@dataclass(slots=True)
class TaskQueueRunnerConfig:
    stub_id: str
    handler_ref: str
    retry_for_refs: tuple[str, ...] = ()
    endpoint: str = DEFAULT_GATEWAY_ENDPOINT
    token: str = ""
    container_id: str = ""
    container_hostname: str = ""
    workspace_id: str = ""
    workspace_name: str = ""
    app_id: str = ""
    lifecycle_hooks: LifecycleHooks = field(default_factory=LifecycleHooks)
    keep_warm_seconds: float = 0.0
    timeout_seconds: float = DEFAULT_RUNNER_TIMEOUT_SECONDS
    control_error_backoff_seconds: float = DEFAULT_TASK_QUEUE_CONTROL_ERROR_BACKOFF_SECONDS
    workers: int = 1
    checkpoint_enabled: bool = False


class TaskQueueControlChannel(Protocol):
    def post(
        self,
        path: str,
        payload: dict[str, JsonValue] | None = None,
    ) -> JsonValue: ...


@dataclass
class TaskQueueRunner:
    config: TaskQueueRunnerConfig
    channel: TaskQueueControlChannel | None = None
    poll_interval_seconds: float = DEFAULT_TASK_QUEUE_POLL_INTERVAL_SECONDS
    _handler: Callable[..., Any] | None = field(default=None, init=False)
    _retry_for: tuple[type[BaseException], ...] | None = field(default=None, init=False)
    _startup_hooks_ran: bool = field(default=False, init=False)
    _handler_lock: threading.RLock = field(default_factory=threading.RLock, init=False)

    @property
    def control(self) -> TaskQueueControlChannel:
        if self.channel is None:
            self.channel = HttpChannel(
                endpoint=self.config.endpoint,
                token=self.config.token or None,
                timeout_seconds=self.config.timeout_seconds,
            )
        return self.channel

    def run_once(self) -> TaskQueueExecutionResult | None:
        self.run_startup_hooks_once()
        invocation = self.pop()
        if invocation is None:
            return None

        initial_monitor = self.monitor(invocation)
        if initial_monitor.cancelled:
            return self.cancel(invocation, "task cancelled before execution")
        if initial_monitor.timed_out:
            return self.timeout(invocation, "task timed out before execution")
        if initial_monitor.complete:
            return None

        started = time.perf_counter()
        try:
            self.run_task_hooks(invocation, LifecycleHookName.Running, TaskStatus.Running)
            result = self.execute_handler(invocation)
        except BaseException as exc:
            duration = time.perf_counter() - started
            formatted = traceback.format_exc()
            self.append_task_log(invocation.task_id, "stderr", formatted)
            self.run_error_hooks(invocation, exc, duration_seconds=duration)
            if self.retryable(exc):
                return self.retry(invocation, exc, duration_seconds=duration)
            return self.fail(invocation, exc, duration_seconds=duration)

        duration = time.perf_counter() - started
        final_monitor = self.monitor(invocation)
        if final_monitor.cancelled:
            return self.cancel(invocation, "task cancelled", duration_seconds=duration)
        if final_monitor.timed_out:
            return self.timeout(invocation, "task timed out", duration_seconds=duration)
        return self.complete(invocation, result, duration_seconds=duration)

    def run_forever(self) -> None:
        watcher = (
            SourceChangeWatcher(hot_reload_root(), self.reload_handler)
            if hot_reload_enabled()
            else None
        )
        if watcher is not None:
            watcher.start()
        try:
            while True:
                if self.run_once() is None:
                    time.sleep(self.poll_interval_seconds)
        finally:
            if watcher is not None:
                watcher.stop()

    def pop(self) -> TaskQueueInvocation | None:
        try:
            response = TaskQueuePopResponse.model_validate(
                self.control.post(
                    "/api/v1/taskqueues/pop",
                    TaskQueuePopRequest(
                        stub_id=self.config.stub_id,
                        container_id=self.config.container_id,
                    ).model_dump(mode="json"),
                )
            )
        except Exception as exc:
            self.report_control_error("pop", exc)
            _sleep_backoff(self.config.control_error_backoff_seconds)
            return None
        if not response.task_msg:
            return None
        message = TaskQueueTaskMessage.model_validate_json(response.task_msg)
        invocation = decode_task_queue_invocation(message.invocation)
        return TaskQueueInvocation(
            task_id=message.task_id,
            args=invocation.args,
            kwargs=invocation.kwargs,
            message=message,
        )

    def execute_handler(self, invocation: TaskQueueInvocation) -> Any:
        stdout = TaskQueueTaskLogStream(self, invocation.task_id, "stdout", sys.stdout)
        stderr = TaskQueueTaskLogStream(self, invocation.task_id, "stderr", sys.stderr)
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            try:
                return invoke_handler(self.handler(), *invocation.args, **invocation.kwargs)
            finally:
                stdout.flush_log()
                stderr.flush_log()

    def monitor(self, invocation: TaskQueueInvocation) -> TaskQueueMonitorResponse:
        return TaskQueueMonitorResponse.model_validate(
            self.control.post(
                "/api/v1/taskqueues/monitor",
                TaskQueueMonitorRequest(
                    task_id=invocation.task_id,
                    stub_id=self.config.stub_id,
                    container_id=self.config.container_id,
                ).model_dump(mode="json"),
            )
        )

    def complete(
        self,
        invocation: TaskQueueInvocation,
        result: Any,
        *,
        duration_seconds: float,
    ) -> TaskQueueExecutionResult:
        payload = serialize_task_queue_result(result)
        response = self.complete_request(
            invocation,
            status=TaskStatus.Complete,
            duration_seconds=duration_seconds,
            result=payload,
        )
        self.run_task_hooks(
            invocation,
            LifecycleHookName.Success,
            response.final_status or TaskStatus.Complete,
            response=response,
            duration_seconds=duration_seconds,
            result_available=True,
        )
        self.run_task_hooks(
            invocation,
            LifecycleHookName.Finish,
            response.final_status or TaskStatus.Complete,
            response=response,
            duration_seconds=duration_seconds,
            result_available=True,
        )
        return TaskQueueExecutionResult(
            invocation=invocation,
            status=TaskStatus.Complete,
            response=response,
            duration_seconds=duration_seconds,
        )

    def retry(
        self,
        invocation: TaskQueueInvocation,
        exc: BaseException,
        *,
        duration_seconds: float,
    ) -> TaskQueueExecutionResult:
        error = f"{type(exc).__name__}: {exc}"
        response = self.complete_request(
            invocation,
            status=TaskStatus.Retry,
            duration_seconds=duration_seconds,
            error=error,
        )
        hook = LifecycleHookName.Retry if response.retry_scheduled else LifecycleHookName.Failure
        self.run_task_hooks(
            invocation,
            hook,
            response.final_status or TaskStatus.Retry,
            response=response,
            duration_seconds=duration_seconds,
            error_type=type(exc).__name__,
            error_message=str(exc),
            retry_scheduled=response.retry_scheduled,
        )
        self.run_task_hooks(
            invocation,
            LifecycleHookName.Finish,
            response.final_status or TaskStatus.Retry,
            response=response,
            duration_seconds=duration_seconds,
            error_type=type(exc).__name__,
            error_message=str(exc),
            retry_scheduled=response.retry_scheduled,
        )
        return TaskQueueExecutionResult(
            invocation=invocation,
            status=TaskStatus.Retry,
            response=response,
            duration_seconds=duration_seconds,
            error=error,
        )

    def fail(
        self,
        invocation: TaskQueueInvocation,
        exc: BaseException,
        *,
        duration_seconds: float,
    ) -> TaskQueueExecutionResult:
        error = f"{type(exc).__name__}: {exc}"
        response = self.complete_request(
            invocation,
            status=TaskStatus.Failed,
            duration_seconds=duration_seconds,
            error=error,
        )
        self.run_task_hooks(
            invocation,
            LifecycleHookName.Failure,
            response.final_status or TaskStatus.Failed,
            response=response,
            duration_seconds=duration_seconds,
            error_type=type(exc).__name__,
            error_message=str(exc),
        )
        self.run_task_hooks(
            invocation,
            LifecycleHookName.Finish,
            response.final_status or TaskStatus.Failed,
            response=response,
            duration_seconds=duration_seconds,
            error_type=type(exc).__name__,
            error_message=str(exc),
        )
        return TaskQueueExecutionResult(
            invocation=invocation,
            status=TaskStatus.Failed,
            response=response,
            duration_seconds=duration_seconds,
            error=error,
        )

    def cancel(
        self,
        invocation: TaskQueueInvocation,
        reason: str,
        *,
        duration_seconds: float = 0.0,
    ) -> TaskQueueExecutionResult:
        response = self.complete_request(
            invocation,
            status=TaskStatus.Cancelled,
            duration_seconds=duration_seconds,
            error=reason,
        )
        self.run_task_hooks(
            invocation,
            LifecycleHookName.Cancelled,
            response.final_status or TaskStatus.Cancelled,
            response=response,
            duration_seconds=duration_seconds,
            error_message=reason,
        )
        self.run_task_hooks(
            invocation,
            LifecycleHookName.Finish,
            response.final_status or TaskStatus.Cancelled,
            response=response,
            duration_seconds=duration_seconds,
            error_message=reason,
        )
        return TaskQueueExecutionResult(
            invocation=invocation,
            status=TaskStatus.Cancelled,
            response=response,
            duration_seconds=duration_seconds,
            error=reason,
        )

    def timeout(
        self,
        invocation: TaskQueueInvocation,
        reason: str,
        *,
        duration_seconds: float = 0.0,
    ) -> TaskQueueExecutionResult:
        response = self.complete_request(
            invocation,
            status=TaskStatus.Timeout,
            duration_seconds=duration_seconds,
            error=reason,
        )
        self.run_task_hooks(
            invocation,
            LifecycleHookName.Timeout,
            response.final_status or TaskStatus.Timeout,
            response=response,
            duration_seconds=duration_seconds,
            error_message=reason,
        )
        self.run_task_hooks(
            invocation,
            LifecycleHookName.Finish,
            response.final_status or TaskStatus.Timeout,
            response=response,
            duration_seconds=duration_seconds,
            error_message=reason,
        )
        return TaskQueueExecutionResult(
            invocation=invocation,
            status=TaskStatus.Timeout,
            response=response,
            duration_seconds=duration_seconds,
            error=reason,
        )

    def complete_request(
        self,
        invocation: TaskQueueInvocation,
        *,
        status: TaskStatus,
        duration_seconds: float,
        result: bytes = b"",
        error: str = "",
    ) -> TaskQueueCompleteResponse:
        response = TaskQueueCompleteResponse.model_validate(
            self.control.post(
                "/api/v1/taskqueues/complete",
                TaskQueueCompleteBody(
                    task_id=invocation.task_id,
                    stub_id=self.config.stub_id,
                    task_duration=duration_seconds,
                    task_status=status,
                    container_id=self.config.container_id,
                    container_hostname=self.config.container_hostname,
                    keep_warm_seconds=self.config.keep_warm_seconds,
                    error=error,
                    value_base64=encode_bytes(result),
                ).model_dump(mode="json"),
            )
        )
        return response

    def append_task_log(self, task_id: str, stream: str, message: str) -> None:
        if not message:
            return
        with contextlib.suppress(Exception):
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
        if stream == "stderr":
            print(message, file=sys.stderr, end="", flush=True)
        else:
            print(message, end="", flush=True)

    def run_startup_hooks_once(self) -> None:
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
            resource_kind=DeploymentKind.TaskQueue,
        )
        run_lifecycle_hooks(
            self.config.lifecycle_hooks,
            LifecycleHookName.Start,
            context,
            log=self.append_container_log,
            capture_output=False,
        )
        restored = wait_for_checkpoint(
            enabled=self.config.checkpoint_enabled,
            workers=self.config.workers,
        )
        if restored is not None:
            self.config.container_id = restored.container_id
            self.config.container_hostname = restored.container_hostname

    def run_error_hooks(
        self,
        invocation: TaskQueueInvocation,
        exc: BaseException,
        *,
        duration_seconds: float,
    ) -> None:
        self.run_task_hooks(
            invocation,
            LifecycleHookName.Error,
            TaskStatus.Failed,
            duration_seconds=duration_seconds,
            error_type=type(exc).__name__,
            error_message=str(exc),
        )

    def run_task_hooks(
        self,
        invocation: TaskQueueInvocation,
        hook: LifecycleHookName,
        status: TaskStatus,
        *,
        response: TaskQueueCompleteResponse | None = None,
        duration_seconds: float = 0.0,
        error_type: str = "",
        error_message: str = "",
        retry_scheduled: bool = False,
        result_available: bool = False,
    ) -> None:
        message = invocation.message
        context = LifecycleTaskContext(
            hook=hook,
            task_id=invocation.task_id,
            status=status,
            stub_id=self.config.stub_id,
            root_task_id=invocation.task_id,
            workspace_id=self.config.workspace_id,
            workspace_name=message.workspace_name if message else self.config.workspace_name,
            app_id=self.config.app_id,
            container_id=self.config.container_id,
            container_hostname=self.config.container_hostname,
            handler=self.config.handler_ref,
            resource_kind=DeploymentKind.TaskQueue,
            attempt_number=response.attempt_number if response else 0,
            max_attempts=response.max_attempts if response else 1,
            duration_seconds=duration_seconds,
            error_type=error_type,
            error_message=error_message,
            retry_scheduled=retry_scheduled or bool(response and response.retry_scheduled),
            result_available=result_available,
        )
        run_lifecycle_hooks(
            self.config.lifecycle_hooks,
            hook,
            context,
            log=lambda stream, message: self.append_task_log(invocation.task_id, stream, message),
        )

    def report_control_error(self, operation: str, exc: Exception) -> None:
        message = "".join(traceback.format_exception_only(type(exc), exc)).strip()
        print(
            f"task queue runner control-plane {operation} failed: {message}",
            file=sys.stderr,
            flush=True,
        )

    def retryable(self, exc: BaseException) -> bool:
        exc_type = type(exc)
        return any(
            isinstance(exc, retry_type) or _same_exception_type(exc_type, retry_type)
            for retry_type in self.retry_for
        )

    @property
    def retry_for(self) -> tuple[type[BaseException], ...]:
        with self._handler_lock:
            if self._retry_for is None:
                self._retry_for = tuple(
                    load_exception_type(ref) for ref in self.config.retry_for_refs
                )
            return self._retry_for

    def handler(self) -> Callable[..., Any]:
        with self._handler_lock:
            if self._handler is None:
                if not self.config.handler_ref:
                    msg = f"task queue stub has no handler: {self.config.stub_id}"
                    raise RuntimeError(msg)
                self._handler = load_callable(self.config.handler_ref)
            return self._handler

    def reload_handler(self) -> None:
        root = hot_reload_root()
        with self._handler_lock:
            evict_user_code_modules(root)
            self._handler = None
            self._retry_for = None
        print("hot reload: task queue handler refreshed", flush=True)


class TaskQueueTaskLogStream(RunnerTaskLogStream):
    def __init__(
        self,
        runner: TaskQueueRunner,
        task_id: str,
        stream: str,
        wrapped: TextIO,
    ) -> None:
        super().__init__(stream, wrapped)
        self.runner = runner
        self.task_id = task_id

    def append_log(self, value: str) -> None:
        self.runner.append_task_log(self.task_id, self.stream, value)


def run_task_queue_once(
    config: TaskQueueRunnerConfig | None = None,
) -> TaskQueueExecutionResult | None:
    runner = TaskQueueRunner(config=config or config_from_env())
    return runner.run_once()


def run_task_queue_forever(config: TaskQueueRunnerConfig | None = None) -> None:
    resolved = config or config_from_env()
    TaskQueueProcessManager(resolved).run()


@dataclass(slots=True)
class TaskQueueProcessManager:
    config: TaskQueueRunnerConfig
    poll_interval_seconds: float = 0.1
    shutdown: threading.Event = field(default_factory=threading.Event)
    processes: list[Process] = field(default_factory=list, init=False)

    def run(self) -> None:
        previous_handlers = {
            handled_signal: signal.signal(handled_signal, self._request_shutdown)
            for handled_signal in (signal.SIGINT, signal.SIGTERM)
        }
        try:
            for index in range(self.config.workers):
                self.processes.append(self._start_worker(index))
            while not self.shutdown.wait(self.poll_interval_seconds):
                failed = next(
                    (process for process in self.processes if process.exitcode is not None),
                    None,
                )
                if failed is not None:
                    msg = f"task queue worker process exited with {failed.exitcode}"
                    raise RuntimeError(msg)
        finally:
            self.stop()
            for handled_signal, previous_handler in previous_handlers.items():
                signal.signal(handled_signal, previous_handler)

    def stop(self) -> None:
        self.shutdown.set()
        for process in self.processes:
            if process.is_alive():
                process.terminate()
        deadline = time.monotonic() + 5.0
        for process in self.processes:
            process.join(timeout=max(deadline - time.monotonic(), 0.0))
        for process in self.processes:
            if process.is_alive():
                process.kill()
                process.join(timeout=1)

    def _start_worker(self, index: int) -> Process:
        process = Process(
            target=_run_task_queue_worker,
            args=(self.config,),
            name=f"taskqueue-worker-{index}",
        )
        process.start()
        return process

    def _request_shutdown(self, signum: int, frame: FrameType | None) -> None:
        del signum, frame
        self.shutdown.set()


def _run_task_queue_worker(config: TaskQueueRunnerConfig) -> None:
    signal.signal(signal.SIGINT, signal.SIG_DFL)
    signal.signal(signal.SIGTERM, signal.SIG_DFL)
    TaskQueueRunner(config=config).run_forever()


def config_from_env(env: dict[str, str] | None = None) -> TaskQueueRunnerConfig:
    source = env or os.environ
    return TaskQueueRunnerConfig(
        stub_id=required_env(source, TASK_QUEUE_STUB_ID_ENV),
        handler_ref=required_env(source, TASK_QUEUE_HANDLER_ENV),
        retry_for_refs=_retry_for_refs(source.get(TASK_QUEUE_RETRY_FOR_ENV, "")),
        endpoint=source.get(GATEWAY_HTTP_URL_ENV) or DEFAULT_GATEWAY_ENDPOINT,
        token=source.get(GATEWAY_TOKEN_ENV, ""),
        container_id=source.get(CONTAINER_ID_ENV) or "local-taskqueue",
        container_hostname=source.get(CONTAINER_HOSTNAME_ENV, socket.gethostname()),
        workspace_id=source.get(WORKSPACE_ID_ENV, ""),
        workspace_name=source.get(WORKSPACE_NAME_ENV, ""),
        app_id=source.get(APP_ID_ENV, ""),
        lifecycle_hooks=lifecycle_hooks_from_env(source.get(LIFECYCLE_HOOKS_ENV)),
        keep_warm_seconds=_float_value(source.get("KEEP_WARM_SECONDS")),
        control_error_backoff_seconds=_float_value(
            source.get(TASK_QUEUE_CONTROL_ERROR_BACKOFF_ENV),
            default=DEFAULT_TASK_QUEUE_CONTROL_ERROR_BACKOFF_SECONDS,
        ),
        workers=_positive_int_value(source.get(TASK_QUEUE_WORKERS_ENV)),
        checkpoint_enabled=truthy_env_value(source.get(CHECKPOINT_ENABLED_ENV)),
    )


def decode_task_queue_invocation(
    payload: TaskQueueSerializedInvocation,
) -> TaskQueueInvocationEnvelope:
    try:
        decoded = cloudpickle.loads(payload.bytes_value())
        return TaskQueueInvocationEnvelope.model_validate(decoded)
    except Exception as exc:
        raise ValueError("invalid task queue invocation envelope") from exc


def serialize_task_queue_result(result: Any) -> bytes:
    if result is None:
        return b""
    value = to_json_value(result)
    return json.dumps(value, separators=(",", ":")).encode("utf-8")


def load_exception_type(reference: str) -> type[BaseException]:
    loaded = load_callable(reference)
    if not isinstance(loaded, type) or not issubclass(loaded, BaseException):
        msg = f"retry_for reference is not an exception class: {reference}"
        raise TypeError(msg)
    return loaded


def _same_exception_type(left: type[BaseException], right: type[BaseException]) -> bool:
    return left.__module__ == right.__module__ and left.__qualname__ == right.__qualname__


def _retry_for_refs(raw: str) -> tuple[str, ...]:
    if not raw.strip():
        return ()
    try:
        decoded = _JSON_VALUE_ADAPTER.validate_json(raw)
    except ValidationError:
        return tuple(item.strip() for item in raw.split(",") if item.strip())
    if not isinstance(decoded, list):
        return ()
    return tuple(item for item in decoded if isinstance(item, str) and item)


def _float_value(raw: str | None, *, default: float = 0.0) -> float:
    try:
        return float(raw if raw is not None else default)
    except ValueError:
        return default


def _positive_int_value(raw: str | None, *, default: int = 1) -> int:
    if raw is None or not raw.strip():
        return default
    try:
        parsed = int(raw)
    except ValueError as exc:
        msg = f"{TASK_QUEUE_WORKERS_ENV} must be an integer"
        raise RuntimeError(msg) from exc
    if parsed < 1:
        msg = f"{TASK_QUEUE_WORKERS_ENV} must be at least 1"
        raise RuntimeError(msg)
    return parsed


def _sleep_backoff(seconds: float) -> None:
    if seconds > 0:
        time.sleep(seconds)


if __name__ == "__main__":
    run_task_queue_forever()
