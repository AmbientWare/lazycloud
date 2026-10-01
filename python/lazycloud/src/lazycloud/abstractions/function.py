from __future__ import annotations

import base64
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from contextlib import AbstractContextManager, nullcontext
from dataclasses import dataclass, field
from functools import update_wrapper
from pathlib import Path
from typing import (
    TYPE_CHECKING,
    Any,
    Generic,
    ParamSpec,
    TypedDict,
    TypeGuard,
    TypeVar,
    overload,
)

from pydantic import ValidationError
from shared.api import (
    Encoding,
    ErrorCode,
    LogEntry,
    Payload,
    Stream,
    SubmitTasksRequest,
)
from shared.api import FunctionSpec as ApiFunctionSpec
from shared.autoscaling import Autoscaler
from shared.callbacks import normalize_callback_url
from shared.deployment_records import (
    DEFAULT_DISK,
    DEFAULT_FUNCTION_AUTHORIZED,
    DEFAULT_FUNCTION_CPU,
    DEFAULT_FUNCTION_MEMORY,
    DEFAULT_FUNCTION_RETRIES,
    DEFAULT_FUNCTION_TIMEOUT_SECONDS,
    DEFAULT_WORKLOAD_PREEMPTIBLE,
    CpuRequest,
    DeploymentSpec,
    MemoryRequest,
    Resources,
    VolumeMount,
)
from shared.deployments import DeploymentKind
from shared.function_payloads import FunctionPayloadEncoding
from shared.gpu import GpuInput, gpu_preference
from shared.image_building.python import python_minor_version
from shared.lifecycle import LifecycleHooks
from shared.placement import ProductRegion
from shared.resources import parse_memory_mib
from shared.serialization import to_json_value
from shared.tasks import DEFAULT_RETRYABLE_TASK_STATUS_SEQUENCE, RetryPolicy, TaskPolicy

from lazycloud._invocation import encode_arguments, prepare_arguments, serialize_result
from lazycloud.abstractions.image import Image
from lazycloud.abstractions.metadata import (
    LifecycleHookInput,
    MachineInput,
    RetryPolicyInput,
    SchemaInput,
    build_resource_metadata,
    lifecycle_hooks,
    retry_policy_config,
)
from lazycloud.abstractions.volume import VolumeExport, volume_mounts
from lazycloud.aio import to_thread
from lazycloud.client_contracts import (
    build_client_contract,
    schema_from_contract_parameters,
    schema_from_contract_return,
)
from lazycloud.clients.api import ApiClient, ApiError
from lazycloud.control import api_client, require_workspace, resolve_control_client_config
from lazycloud.env import called_on_import, is_local
from lazycloud.exceptions import (
    FunctionNotDeployedError,
    MapSubmissionError,
    SdkError,
    UnsupportedFeatureError,
)
from lazycloud.references import dotted_reference
from lazycloud.session.task import FunctionCall, Task
from lazycloud.terminal import Terminal, TerminalStep
from lazycloud.values import cloudpickle_bytes

if TYPE_CHECKING:
    from shared.api import Deployment


# Inputs per submit request; the API rejects larger batches.
MAX_SUBMIT_BATCH = 1000

P = ParamSpec("P")
R = TypeVar("R")


class FunctionOperationError(SdkError):
    pass


class FunctionOptions(TypedDict, total=False):
    image: Image | None
    name: str | None
    cpu: CpuRequest | None
    memory: MemoryRequest | None
    disk: str | None
    gpu: GpuInput
    gpu_count: int
    timeout_seconds: int | None
    concurrency: int
    in_process: bool
    cron: str | None
    keep_warm: int | None
    max_pending_tasks: int | None
    autoscaler: Autoscaler | Mapping[str, Any] | None
    retries: int
    retry_policy: RetryPolicyInput
    retry_delay_seconds: float
    callback_url: str | None
    authorized: bool | None
    env: dict[str, str] | None
    secrets: list[str] | None
    volumes: Iterable[VolumeMount | VolumeExport] | None
    on_start: LifecycleHookInput
    on_running: LifecycleHookInput
    on_success: LifecycleHookInput
    on_error: LifecycleHookInput
    on_retry: LifecycleHookInput
    on_failure: LifecycleHookInput
    on_finish: LifecycleHookInput
    task_policy: TaskPolicy | Mapping[str, Any] | None
    inputs: SchemaInput
    outputs: SchemaInput
    docker_enabled: bool
    preemptible: bool
    region: str | None
    availability_zone: str
    machine: MachineInput
    metadata: dict[str, Any] | None


@dataclass
class Function(Generic[P, R]):
    func: Callable[P, R]
    _app_slug: str
    image: Image = field(default_factory=Image)
    name: str | None = None
    cpu: CpuRequest | None = DEFAULT_FUNCTION_CPU
    memory: MemoryRequest | None = DEFAULT_FUNCTION_MEMORY
    disk: str | None = None
    gpu: GpuInput = None
    gpu_count: int = 0
    timeout_seconds: int | None = DEFAULT_FUNCTION_TIMEOUT_SECONDS
    concurrency: int = 1
    in_process: bool = False
    cron: str | None = None
    keep_warm: int | None = None
    max_pending_tasks: int | None = None
    autoscaler: Autoscaler | Mapping[str, Any] | None = None
    retries: int = DEFAULT_FUNCTION_RETRIES
    retry_policy: RetryPolicyInput = None
    retry_delay_seconds: float = 0.0
    callback_url: str | None = None
    authorized: bool | None = DEFAULT_FUNCTION_AUTHORIZED
    env: dict[str, str] = field(default_factory=dict)
    secrets: list[str] = field(default_factory=list)
    volumes: list[VolumeMount] = field(default_factory=list)
    on_start: LifecycleHookInput = None
    on_running: LifecycleHookInput = None
    on_success: LifecycleHookInput = None
    on_error: LifecycleHookInput = None
    on_retry: LifecycleHookInput = None
    on_failure: LifecycleHookInput = None
    on_finish: LifecycleHookInput = None
    task_policy: TaskPolicy | None = None
    inputs: SchemaInput = None
    outputs: SchemaInput = None
    docker_enabled: bool = False
    preemptible: bool = DEFAULT_WORKLOAD_PREEMPTIBLE
    region: str | None = None
    availability_zone: str = ""
    machine: MachineInput = None
    metadata: dict[str, Any] = field(default_factory=dict)
    client: ApiClient | None = field(default=None, init=False, repr=False)
    workspace: str | None = field(default=None, init=False)
    terminal: Terminal | None = field(
        default_factory=lambda: Terminal(default_enabled=is_local()), init=False, repr=False
    )

    def __post_init__(self) -> None:
        update_wrapper(self, self.func)

    @property
    def resource_name(self) -> str:
        return self.name or self.func.__name__

    def __call__(self, *args: P.args, **kwargs: P.kwargs) -> R:
        return self.local(*args, **kwargs)

    def local(self, *args: P.args, **kwargs: P.kwargs) -> R:
        if self.inputs is None:
            return serialize_result(self.func, self.func(*args, **kwargs), self.outputs)
        return self.invoke_arguments(args, kwargs)

    def invoke_arguments(
        self,
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
        *,
        encoding: FunctionPayloadEncoding = FunctionPayloadEncoding.Cloudpickle,
    ) -> R:
        prepared_args, prepared_kwargs = prepare_arguments(
            self.func, args, kwargs, self.inputs, encoding=encoding
        )
        return serialize_result(
            self.func, self.func(*prepared_args, **prepared_kwargs), self.outputs
        )

    def spec(self) -> DeploymentSpec:
        kind = DeploymentKind.Function
        client_contract = build_client_contract(
            self.func,
            kind=kind,
            inputs=self.inputs,
            outputs=self.outputs,
        )
        return DeploymentSpec(
            name=self.resource_name,
            kind=kind,
            handler=dotted_reference(self.func),
            cron=self.cron,
            image=self.image.spec(),
            resources=Resources(
                region=ProductRegion(self.region) if self.region is not None else None,
                availability_zone=self.availability_zone,
                cpu=self.cpu,
                memory=self.memory,
                disk=self.disk or DEFAULT_DISK,
                gpu=list(gpu_preference(self.gpu)),
                gpu_count=self.gpu_count,
                timeout_seconds=self._effective_timeout_seconds(),
                concurrency=self.concurrency,
                keep_warm=self.keep_warm,
                preemptible=self.preemptible,
            ),
            env=self.env,
            secrets=self.secrets,
            volumes=list(self.volumes),
            retry_policy=retry_policy_config(
                self.retry_policy,
                retries=self.retries,
                retry_delay_seconds=self.retry_delay_seconds,
            ),
            lifecycle_hooks=lifecycle_hooks(
                on_start=self.on_start,
                on_running=self.on_running,
                on_success=self.on_success,
                on_error=self.on_error,
                on_retry=self.on_retry,
                on_failure=self.on_failure,
                on_finish=self.on_finish,
            ),
            metadata=build_resource_metadata(
                app=self._app_slug,
                autoscaler=self.autoscaler,
                max_pending_tasks=self.max_pending_tasks,
                callback_url=self.callback_url,
                authorized=self.authorized,
                in_process=self.in_process,
                task_policy=self.task_policy,
                inputs=(
                    self.inputs
                    if self.inputs is not None
                    else schema_from_contract_parameters(client_contract)
                ),
                outputs=(
                    self.outputs
                    if self.outputs is not None
                    else schema_from_contract_return(client_contract)
                ),
                docker_enabled=self.docker_enabled,
                machine=self.machine,
                extra=self.metadata,
            ),
            client_contract=client_contract,
        )

    def unsupported_options(self) -> list[str]:
        """Declared options the platform cannot run yet, by name."""
        found: list[str] = []
        if self.gpu is not None or self.gpu_count:
            found.append("gpu")
        declared = {
            "disk": self.disk is not None,
            "volumes": bool(self.volumes),
            "authorized": self.authorized is not DEFAULT_FUNCTION_AUTHORIZED,
            "docker_enabled": self.docker_enabled,
            "preemptible": self.preemptible is not DEFAULT_WORKLOAD_PREEMPTIBLE,
            "region": self.region is not None,
            "availability_zone": bool(self.availability_zone),
            "machine": self.machine is not None,
            "metadata": bool(self.metadata),
        }
        found.extend(name for name, present in declared.items() if present)
        policy = self._retry_policy()
        if policy.retry_on_statuses != DEFAULT_RETRYABLE_TASK_STATUS_SEQUENCE:
            found.append("retry_policy.retry_on_statuses")
        found.extend(f"image {name}" for name in _unsupported_image_options(self.image))
        return found

    def require_supported(self) -> None:
        unsupported = self.unsupported_options()
        if unsupported:
            raise UnsupportedFeatureError(f"function {self.resource_name}", unsupported)

    def function_spec(self, *, handler: str, source_sha256: str) -> ApiFunctionSpec:
        """The API definition of this function for one uploaded source archive."""
        self.require_supported()
        policy = self._retry_policy()
        spec: dict[str, Any] = {
            "name": self.resource_name,
            "handler": handler,
            "source": {"sha256": source_sha256},
            "image": {"python_version": python_minor_version(self.image.python_version)},
            "resources": _resources(self.cpu, self.memory),
            "retry_policy": policy.model_dump(
                mode="json",
                include={"max_attempts", "delay_seconds", "backoff", "max_delay_seconds"},
                exclude_none=True,
            ),
            "concurrency": self.concurrency,
        }
        timeout_seconds = self._effective_timeout_seconds()
        if timeout_seconds is not None:
            spec["timeout_seconds"] = timeout_seconds
        if self.keep_warm is not None:
            spec["keep_warm_seconds"] = self.keep_warm
        if self.autoscaler is not None:
            spec["autoscaler"] = Autoscaler.model_validate(self.autoscaler).model_dump(
                include={"min_containers", "max_containers", "tasks_per_container"}
            )
        if self.max_pending_tasks is not None:
            spec["max_pending_tasks"] = self.max_pending_tasks
        if self.env:
            spec["environment"] = dict(self.env)
        if self.cron:
            spec["cron"] = self.cron
        if self.secrets:
            spec["secrets"] = list(dict.fromkeys(self.secrets))
        if self.in_process:
            spec["in_process"] = True
        try:
            callback_url = normalize_callback_url(self.callback_url)
            hooks = self._lifecycle_hooks()
        except (TypeError, ValueError) as exc:
            msg = f"function {self.resource_name} has invalid options: {exc}"
            raise FunctionOperationError(msg) from exc
        if callback_url is not None:
            spec["callback_url"] = callback_url
        if hooks.configured:
            spec["lifecycle_hooks"] = {
                name: list(refs) for name, refs in hooks.model_dump().items() if refs
            }
        try:
            return ApiFunctionSpec.model_validate(spec)
        except ValidationError as exc:
            msg = f"function {self.resource_name} has invalid options: {exc}"
            raise FunctionOperationError(msg) from exc

    def deploy(
        self,
        *,
        workspace: str | None = None,
        source_root: str | Path | None = None,
    ) -> Deployment:
        """Deploy this function into its app without touching the app's other functions."""
        from lazycloud.session.deployment import AppFunctions, deploy_functions

        client, selected_workspace = self._session(workspace)
        return deploy_functions(
            [AppFunctions(app=self._app_slug, functions=(self,))],
            client=client,
            workspace=selected_workspace,
            source_root=source_root,
            terminal=self.terminal,
        )[0]

    def serve(self, **_: object) -> None:
        raise UnsupportedFeatureError(f"function {self.resource_name}", ["serve"])

    def shell(self, **_: object) -> None:
        raise UnsupportedFeatureError(f"function {self.resource_name}", ["shell"])

    def remote(self, *args: P.args, **kwargs: P.kwargs) -> R:
        """Run one task remotely, print its output as it arrives and return its value.

        A dropped connection never cancels the task; the call resumes following it.
        """
        return self._remote_call(args, kwargs)

    async def async_remote(self, *args: P.args, **kwargs: P.kwargs) -> R:
        return await to_thread(self._remote_call, args, kwargs)

    @overload
    def spawn(self, *args: P.args, **kwargs: P.kwargs) -> FunctionCall[R]: ...

    @overload
    def spawn(self, *args: Any, **kwargs: Any) -> FunctionCall[R]: ...

    def spawn(self, *args: Any, **kwargs: Any) -> FunctionCall[R]:
        return FunctionCall(self._submit([self._input(args, kwargs)])[0])

    @overload
    async def async_spawn(self, *args: P.args, **kwargs: P.kwargs) -> FunctionCall[R]: ...

    @overload
    async def async_spawn(self, *args: Any, **kwargs: Any) -> FunctionCall[R]: ...

    async def async_spawn(self, *args: Any, **kwargs: Any) -> FunctionCall[R]:
        return await to_thread(self.spawn, *args, **kwargs)

    def spawn_map(self, inputs: Sequence[Any]) -> list[FunctionCall[R]]:
        """Submit one task per input, in batches, and return the calls in input order."""
        if not inputs:
            return []
        payloads = [self._input(_map_args(value), {}) for value in inputs]
        return [FunctionCall(task) for task in self._submit(payloads)]

    def map(self, inputs: Sequence[Any]) -> Iterator[R | None]:
        """Yield each input's value in input order; a failed task yields None."""
        for call in self.spawn_map(inputs):
            try:
                yield call.get()
            except Exception as exc:
                self._error(f"Task {call.task_id} failed during map: {exc}")
                yield None

    def submit_json(self, args: Sequence[Any], kwargs: Mapping[str, Any]) -> Task:
        """Submit one task with JSON arguments; its result comes back as JSON."""
        payload = Payload(
            encoding=Encoding.json,
            value={"args": to_json_value(list(args)), "kwargs": to_json_value(dict(kwargs))},
        )
        return self._submit([payload])[0]

    def run_task(self, task: Task) -> Any:
        """Follow a submitted task's output until it finishes, then return its outcome."""
        with self._task_step() as step:
            step.update(f"{task.task_id[:8]} submitted")
            try:
                task.follow_logs(self._print_log)
            except SdkError as exc:
                self._warn(f"output of task {task.task_id} is unavailable: {exc}")
            if self.terminal is not None:
                self.terminal.flush_remote_output()
            view = task.wait()
            step.update(f"{task.task_id[:8]} {view.status.value}")
        return task.outcome(view)

    def _remote_call(self, args: tuple[Any, ...], kwargs: Mapping[str, Any]) -> R:
        return self.run_task(self._submit([self._input(args, kwargs)])[0])

    def _input(self, args: tuple[Any, ...], kwargs: Mapping[str, Any]) -> Payload:
        encoded_args, encoded_kwargs = encode_arguments(self.func, args, kwargs, self.inputs)
        data = cloudpickle_bytes({"args": list(encoded_args), "kwargs": encoded_kwargs})
        return Payload.model_validate(
            {"encoding": Encoding.cloudpickle, "data": base64.b64encode(data)}
        )

    def _submit(self, inputs: list[Payload]) -> list[Task]:
        if called_on_import():
            msg = "remote function invocation is unavailable while importing user code"
            raise FunctionOperationError(msg)
        self.require_supported()
        client, workspace = self._session()
        tasks: list[Task] = []
        for start in range(0, len(inputs), MAX_SUBMIT_BATCH):
            request = SubmitTasksRequest(inputs=inputs[start : start + MAX_SUBMIT_BATCH])
            try:
                response = client.submit_tasks(
                    workspace, self._app_slug, self.resource_name, request
                )
            except SdkError as exc:
                error: SdkError = exc
                if isinstance(exc, ApiError) and exc.code is ErrorCode.not_found:
                    error = FunctionNotDeployedError(self._app_slug, self.resource_name, workspace)
                if not tasks:
                    raise error from exc
                raise MapSubmissionError(
                    [FunctionCall(task) for task in tasks], len(inputs), error
                ) from exc
            tasks.extend(Task(str(item.id), workspace, client) for item in response.tasks)
        return tasks

    def _session(self, workspace: str | None = None) -> tuple[ApiClient, str]:
        config = resolve_control_client_config(workspace=workspace or self.workspace)
        if self.client is None:
            self.client = api_client(config)
        return self.client, require_workspace(config)

    def _lifecycle_hooks(self) -> LifecycleHooks:
        return lifecycle_hooks(
            on_start=self.on_start,
            on_running=self.on_running,
            on_success=self.on_success,
            on_error=self.on_error,
            on_retry=self.on_retry,
            on_failure=self.on_failure,
            on_finish=self.on_finish,
        )

    def _retry_policy(self) -> RetryPolicy:
        policy = retry_policy_config(
            self.retry_policy,
            retries=self.retries,
            retry_delay_seconds=self.retry_delay_seconds,
        )
        return policy if policy is not None else RetryPolicy(max_attempts=1)

    def _print_log(self, entry: LogEntry) -> None:
        if self.terminal is None:
            return
        data = entry.data if entry.data.endswith("\n") else entry.data + "\n"
        stream = "stdout" if entry.stream is Stream.stdout else "stderr"
        self.terminal.remote_output(data, stream=stream)

    def _task_step(self) -> AbstractContextManager[TerminalStep]:
        if self.terminal is None:
            return nullcontext(TerminalStep(name="Task", terminal=Terminal(quiet=True)))
        return self.terminal.step("Task", "submitting")

    def _error(self, message: str) -> None:
        terminal = self.terminal or Terminal()
        terminal.error(message)

    def _warn(self, message: str) -> None:
        if self.terminal is not None:
            self.terminal.warn(message)

    def _effective_timeout_seconds(self) -> int | None:
        if self.task_policy is not None and self.task_policy.timeout_seconds is not None:
            return self.task_policy.timeout_seconds
        return self.timeout_seconds


def _map_args(input_value: Any) -> tuple[Any, ...]:
    if _is_invocation_tuple(input_value) or _is_invocation_list(input_value):
        return tuple(input_value)
    return (input_value,)


def _resources(cpu: Any, memory: Any) -> dict[str, int]:
    """Reservations, plus ceilings when `cpu` or `memory` is a `(reserve, limit)` pair."""
    cpu = DEFAULT_FUNCTION_CPU if cpu is None else cpu
    memory = DEFAULT_FUNCTION_MEMORY if memory is None else memory
    resources: dict[str, int] = {}
    if isinstance(cpu, tuple | list):
        resources["cpu_millis"] = round(float(cpu[0]) * 1000)
        resources["cpu_limit_millis"] = round(float(cpu[1]) * 1000)
    else:
        resources["cpu_millis"] = round(float(cpu) * 1000)
    if isinstance(memory, tuple | list):
        resources["memory_mib"] = parse_memory_mib(memory[0])
        resources["memory_limit_mib"] = parse_memory_mib(memory[1])
    else:
        resources["memory_mib"] = parse_memory_mib(memory)
    return resources


def _unsupported_image_options(image: Image) -> list[str]:
    declared = image.spec().model_dump()
    baseline = Image(python_version=image.python_version).spec().model_dump()
    found = [name for name, value in declared.items() if baseline.get(name) != value]
    if image.python_version.startswith("micromamba"):
        found.append("micromamba python")
    return found


def _is_invocation_list(value: object) -> TypeGuard[list[object]]:
    return isinstance(value, list)


def _is_invocation_tuple(value: object) -> TypeGuard[tuple[object, ...]]:
    return isinstance(value, tuple)


@overload
def _function(
    func: Callable[P, R],
    *,
    _app_slug: str,
    image: Image | None = None,
    name: str | None = None,
    cpu: CpuRequest | None = DEFAULT_FUNCTION_CPU,
    memory: MemoryRequest | None = DEFAULT_FUNCTION_MEMORY,
    disk: str | None = None,
    gpu: GpuInput = None,
    gpu_count: int = 0,
    timeout_seconds: int | None = DEFAULT_FUNCTION_TIMEOUT_SECONDS,
    concurrency: int = 1,
    in_process: bool = False,
    cron: str | None = None,
    keep_warm: int | None = None,
    max_pending_tasks: int | None = None,
    autoscaler: Autoscaler | Mapping[str, Any] | None = None,
    retries: int = DEFAULT_FUNCTION_RETRIES,
    retry_policy: RetryPolicy | Mapping[str, Any] | None = None,
    retry_delay_seconds: float = 0.0,
    callback_url: str | None = None,
    authorized: bool | None = DEFAULT_FUNCTION_AUTHORIZED,
    env: dict[str, str] | None = None,
    secrets: list[str] | None = None,
    volumes: Iterable[VolumeMount | VolumeExport] | None = None,
    on_start: LifecycleHookInput = None,
    on_running: LifecycleHookInput = None,
    on_success: LifecycleHookInput = None,
    on_error: LifecycleHookInput = None,
    on_retry: LifecycleHookInput = None,
    on_failure: LifecycleHookInput = None,
    on_finish: LifecycleHookInput = None,
    task_policy: TaskPolicy | Mapping[str, Any] | None = None,
    inputs: SchemaInput = None,
    outputs: SchemaInput = None,
    docker_enabled: bool = False,
    preemptible: bool = DEFAULT_WORKLOAD_PREEMPTIBLE,
    region: str | None = None,
    availability_zone: str = "",
    machine: MachineInput = None,
    metadata: dict[str, Any] | None = None,
) -> Function[P, R]: ...


@overload
def _function(
    func: None = None,
    *,
    _app_slug: str,
    image: Image | None = None,
    name: str | None = None,
    cpu: CpuRequest | None = DEFAULT_FUNCTION_CPU,
    memory: MemoryRequest | None = DEFAULT_FUNCTION_MEMORY,
    disk: str | None = None,
    gpu: GpuInput = None,
    gpu_count: int = 0,
    timeout_seconds: int | None = DEFAULT_FUNCTION_TIMEOUT_SECONDS,
    concurrency: int = 1,
    in_process: bool = False,
    cron: str | None = None,
    keep_warm: int | None = None,
    max_pending_tasks: int | None = None,
    autoscaler: Autoscaler | Mapping[str, Any] | None = None,
    retries: int = DEFAULT_FUNCTION_RETRIES,
    retry_policy: RetryPolicy | Mapping[str, Any] | None = None,
    retry_delay_seconds: float = 0.0,
    callback_url: str | None = None,
    authorized: bool | None = DEFAULT_FUNCTION_AUTHORIZED,
    env: dict[str, str] | None = None,
    secrets: list[str] | None = None,
    volumes: Iterable[VolumeMount | VolumeExport] | None = None,
    on_start: LifecycleHookInput = None,
    on_running: LifecycleHookInput = None,
    on_success: LifecycleHookInput = None,
    on_error: LifecycleHookInput = None,
    on_retry: LifecycleHookInput = None,
    on_failure: LifecycleHookInput = None,
    on_finish: LifecycleHookInput = None,
    task_policy: TaskPolicy | Mapping[str, Any] | None = None,
    inputs: SchemaInput = None,
    outputs: SchemaInput = None,
    docker_enabled: bool = False,
    preemptible: bool = DEFAULT_WORKLOAD_PREEMPTIBLE,
    region: str | None = None,
    availability_zone: str = "",
    machine: MachineInput = None,
    metadata: dict[str, Any] | None = None,
) -> Callable[[Callable[P, R]], Function[P, R]]: ...


def _function(
    func: Callable[P, R] | None = None,
    *,
    _app_slug: str,
    image: Image | None = None,
    name: str | None = None,
    cpu: CpuRequest | None = DEFAULT_FUNCTION_CPU,
    memory: MemoryRequest | None = DEFAULT_FUNCTION_MEMORY,
    disk: str | None = None,
    gpu: GpuInput = None,
    gpu_count: int = 0,
    timeout_seconds: int | None = DEFAULT_FUNCTION_TIMEOUT_SECONDS,
    concurrency: int = 1,
    in_process: bool = False,
    cron: str | None = None,
    keep_warm: int | None = None,
    max_pending_tasks: int | None = None,
    autoscaler: Autoscaler | Mapping[str, Any] | None = None,
    retries: int = DEFAULT_FUNCTION_RETRIES,
    retry_policy: RetryPolicy | Mapping[str, Any] | None = None,
    retry_delay_seconds: float = 0.0,
    callback_url: str | None = None,
    authorized: bool | None = DEFAULT_FUNCTION_AUTHORIZED,
    env: dict[str, str] | None = None,
    secrets: list[str] | None = None,
    volumes: Iterable[VolumeMount | VolumeExport] | None = None,
    on_start: LifecycleHookInput = None,
    on_running: LifecycleHookInput = None,
    on_success: LifecycleHookInput = None,
    on_error: LifecycleHookInput = None,
    on_retry: LifecycleHookInput = None,
    on_failure: LifecycleHookInput = None,
    on_finish: LifecycleHookInput = None,
    task_policy: TaskPolicy | Mapping[str, Any] | None = None,
    inputs: SchemaInput = None,
    outputs: SchemaInput = None,
    docker_enabled: bool = False,
    preemptible: bool = DEFAULT_WORKLOAD_PREEMPTIBLE,
    region: str | None = None,
    availability_zone: str = "",
    machine: MachineInput = None,
    metadata: dict[str, Any] | None = None,
) -> Callable[[Callable[P, R]], Function[P, R]] | Function[P, R]:
    def decorate(target: Callable[P, R]) -> Function[P, R]:
        return Function(
            target,
            _app_slug=_app_slug,
            image=image or Image(),
            name=name,
            cpu=cpu,
            memory=memory,
            disk=disk,
            gpu=gpu,
            gpu_count=gpu_count,
            timeout_seconds=timeout_seconds,
            concurrency=concurrency,
            in_process=in_process,
            cron=cron,
            keep_warm=keep_warm,
            max_pending_tasks=max_pending_tasks,
            autoscaler=autoscaler,
            retries=retries,
            retry_policy=retry_policy,
            retry_delay_seconds=retry_delay_seconds,
            callback_url=callback_url,
            authorized=authorized,
            env=env or {},
            secrets=secrets or [],
            volumes=volume_mounts(volumes or ()),
            on_start=on_start,
            on_running=on_running,
            on_success=on_success,
            on_error=on_error,
            on_retry=on_retry,
            on_failure=on_failure,
            on_finish=on_finish,
            task_policy=_normalized_task_policy(task_policy),
            inputs=inputs,
            outputs=outputs,
            docker_enabled=docker_enabled,
            preemptible=preemptible,
            region=region,
            availability_zone=availability_zone,
            machine=machine,
            metadata=metadata or {},
        )

    if func is None:
        return decorate
    return decorate(func)


def _normalized_task_policy(
    value: TaskPolicy | Mapping[str, Any] | None,
) -> TaskPolicy | None:
    if value is None:
        return None
    return TaskPolicy.model_validate(value)


__all__ = [
    "Function",
    "FunctionOperationError",
    "FunctionOptions",
    "VolumeExport",
    "_function",
]
