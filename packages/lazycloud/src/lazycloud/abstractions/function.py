from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from contextlib import AbstractContextManager, nullcontext
from dataclasses import dataclass, field
from functools import update_wrapper
from pathlib import Path
from typing import (
    Any,
    Generic,
    ParamSpec,
    Protocol,
    TypedDict,
    TypeVar,
    overload,
    runtime_checkable,
)

from pydantic import ValidationError
from shared.autoscaling import QueueDepthAutoscaler
from shared.deployment_records import (
    DEFAULT_DISK,
    DEFAULT_FUNCTION_AUTHORIZED,
    DEFAULT_FUNCTION_CPU,
    DEFAULT_FUNCTION_MEMORY,
    DEFAULT_FUNCTION_RETRIES,
    DEFAULT_FUNCTION_TIMEOUT_SECONDS,
    CpuRequest,
    DeploymentSpec,
    MemoryRequest,
    Resources,
    VolumeMount,
)
from shared.deployments import DeploymentKind
from shared.function_payloads import (
    FunctionCloudpickleInvocation,
    FunctionInvocationArguments,
    FunctionInvocationPayload,
)
from shared.gpu import GpuInput, gpu_preference
from shared.http.functions import (
    FUNCTION_CALL_REF_MARKER,
    FunctionCallDependency,
    FunctionInvokeResponse,
)
from shared.http.gateway import DeployStubResponse
from shared.placement import ProductRegion
from shared.task_context import current_root_task_id, current_task_id
from shared.tasks import RetryPolicy, TaskPolicy

from lazycloud.abstractions.image import Image
from lazycloud.abstractions.metadata import (
    LifecycleHookInput,
    PoolInput,
    RetryPolicyInput,
    SchemaInput,
    build_resource_metadata,
    lifecycle_hooks,
    retry_policy_config,
)
from lazycloud.abstractions.serve import sync_local_workspace
from lazycloud.abstractions.shell import Shell, ShellSession
from lazycloud.abstractions.volume import VolumeExport, volume_mounts
from lazycloud.aio import to_thread
from lazycloud.client_contracts import (
    build_client_contract,
    schema_from_contract_parameters,
    schema_from_contract_return,
)
from lazycloud.clients.function.control import FunctionControlClient
from lazycloud.control import ControlClientConfig, resolve_control_client_config
from lazycloud.control_clients import gateway_control_client
from lazycloud.env import called_on_import, is_local
from lazycloud.references import dotted_reference
from lazycloud.session.deployment import DeploymentClient, DeploymentControlClient
from lazycloud.session.task import FunctionCall, TaskClient, TaskOperationError
from lazycloud.terminal import Terminal, TerminalStep
from lazycloud.values import cloudpickle_bytes

P = ParamSpec("P")
R = TypeVar("R")


class _FunctionClient(Protocol):
    def invoke(
        self,
        stub_id: str,
        invocation: FunctionInvocationPayload,
        *,
        detached: bool = False,
        parent_task_id: str = "",
        root_task_id: str = "",
        dependencies: list[FunctionCallDependency] | None = None,
    ) -> Iterator[FunctionInvokeResponse]: ...


class FunctionOperationError(RuntimeError):
    pass


@runtime_checkable
class InvocationMapping(Protocol):
    def items(self) -> Iterable[tuple[Any, Any]]: ...


@runtime_checkable
class InvocationIterable(Protocol):
    def __iter__(self) -> Iterator[Any]: ...


@dataclass(frozen=True, slots=True)
class SerializedFunctionInvocation:
    payload: FunctionInvocationPayload
    dependencies: list[FunctionCallDependency]


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
    autoscaler: QueueDepthAutoscaler | Mapping[str, Any] | None
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
    pool: PoolInput
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
    autoscaler: QueueDepthAutoscaler | Mapping[str, Any] | None = None
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
    preemptible: bool = False
    region: str | None = None
    pool: PoolInput = None
    metadata: dict[str, Any] = field(default_factory=dict)
    stub_id: str = field(default="", init=False)
    client: _FunctionClient | None = field(default=None, init=False, repr=False)
    deployment_client: DeploymentControlClient | None = field(
        default=None,
        init=False,
        repr=False,
    )
    endpoint: str | None = field(default=None, init=False)
    token: str | None = field(default=None, init=False, repr=False)
    timeout: float = field(default=10.0, init=False)
    terminal: Terminal | None = field(
        default_factory=lambda: Terminal(default_enabled=False), init=False, repr=False
    )

    def __post_init__(self) -> None:
        update_wrapper(self, self.func)

    @property
    def resource_name(self) -> str:
        return self.name or self.func.__name__

    @property
    def control_client(self) -> _FunctionClient:
        if self.client is None:
            self.client = _default_function_client(self._config())
        return self.client

    def _config(self) -> ControlClientConfig:
        return resolve_control_client_config(
            endpoint=self.endpoint,
            token=self.token,
            timeout_seconds=self.timeout,
        )

    def __call__(self, *args: P.args, **kwargs: P.kwargs) -> R:
        return self.local(*args, **kwargs)

    def local(self, *args: P.args, **kwargs: P.kwargs) -> R:
        return self.func(*args, **kwargs)

    def configure(
        self,
        *,
        image: Image | None = None,
        cpu: CpuRequest | None = None,
        memory: MemoryRequest | None = None,
        disk: str | None = None,
        gpu: GpuInput = None,
        gpu_count: int | None = None,
        env: Mapping[str, str] | None = None,
        secrets: Iterable[str] | None = None,
        region: str | None = None,
        pool: PoolInput = None,
        preemptible: bool | None = None,
    ) -> Function[P, R]:
        """Apply explicit authoring overrides before preparation or invocation."""
        if image is not None:
            self.image = image
        if cpu is not None:
            self.cpu = cpu
        if memory is not None:
            self.memory = memory
        if gpu is not None:
            self.gpu = gpu
        if gpu_count is not None:
            self.gpu_count = gpu_count
        if env:
            self.env.update(env)
        if secrets:
            self.secrets.extend(secret for secret in secrets if secret not in self.secrets)
        if pool is not None:
            self.pool = pool
        if region is not None:
            self.region = region
        if preemptible is not None:
            self.preemptible = preemptible
        return self

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
                pool=self.pool,
                extra=self.metadata,
            ),
            client_contract=client_contract,
        )

    def prepare(
        self,
        *,
        workspace: str | None = None,
        source_root: str | Path | None = None,
    ) -> str:
        try:
            response = DeploymentClient(
                client=self.deployment_client,
                workspace=workspace,
                endpoint=self.endpoint,
                token=self.token,
                timeout_seconds=self.timeout,
                sync_source=True,
                terminal=self.terminal,
            ).prepare(
                self.spec(),
                workspace=workspace,
                image=self.image,
                source_root=source_root,
            )
        except RuntimeError as exc:
            raise FunctionOperationError(str(exc)) from exc
        if not response.stub_id:
            msg = "deployment prepare did not return a function stub_id"
            raise FunctionOperationError(msg)
        self.stub_id = response.stub_id
        return self.stub_id

    def deploy(
        self,
        *,
        name: str | None = None,
        workspace: str | None = None,
        source_root: str | Path | None = None,
    ) -> DeployStubResponse:
        try:
            response = DeploymentClient(
                client=self.deployment_client,
                workspace=workspace,
                endpoint=self.endpoint,
                token=self.token,
                timeout_seconds=self.timeout,
                sync_source=True,
                terminal=self.terminal,
            ).create(
                self.spec(),
                name=name,
                workspace=workspace,
                image=self.image,
                source_root=source_root,
            )
        except RuntimeError as exc:
            raise FunctionOperationError(str(exc)) from exc
        self.stub_id = response.stub_id or self.stub_id
        return response

    def shell(
        self,
        *,
        workspace: str | None = None,
        container_id: str | None = None,
        sync_dir: str | None = None,
    ) -> ShellSession:
        shell = Shell(
            workspace=workspace,
            endpoint=self.endpoint,
            token=self.token,
            timeout_seconds=self.timeout,
        )
        if container_id:
            session = shell.create_existing(container_id)
        else:
            session = shell.create_standalone(self.stub_id or self.prepare(workspace=workspace))
        if sync_dir:
            self._sync_shell_dir(session.container_id, sync_dir)
        return session

    def _sync_shell_dir(self, container_id: str, sync_dir: str) -> None:
        sync_local_workspace(
            container_id=container_id,
            local_dir=sync_dir,
            gateway_client=gateway_control_client(self._config()),
            terminal=self.terminal,
        )

    def remote(self, *args: P.args, **kwargs: P.kwargs) -> R:
        self._reject_import_invocation()
        return self._remote_call(*args, **kwargs)

    def _remote_call(self, *args: P.args, **kwargs: P.kwargs) -> R:
        self._ensure_invokable()
        with self._task_step() as step:
            response = self._invoke_serialized(detached=False, args=args, kwargs=kwargs, step=step)
            if not response.task_id:
                raise FunctionOperationError(response.output or "function invocation failed")
            if not response.done:
                msg = f"function invocation ended before task {response.task_id} completed"
                raise FunctionOperationError(msg)
            call = self._call_from_response(response)
            try:
                return call.get(timeout_seconds=self._effective_timeout_seconds())
            except TaskOperationError as exc:
                raise FunctionOperationError(str(exc)) from exc

    @overload
    def spawn(self, *args: P.args, **kwargs: P.kwargs) -> FunctionCall[R]: ...

    @overload
    def spawn(self, *args: Any, **kwargs: Any) -> FunctionCall[R]: ...

    def spawn(self, *args: Any, **kwargs: Any) -> FunctionCall[R]:
        self._reject_import_invocation()
        self._ensure_invokable()
        response = self._invoke(True, *args, **kwargs)
        if not response.task_id:
            raise FunctionOperationError(response.output or "function invocation failed")
        return self._call_from_response(response)

    def spawn_map(self, inputs: Sequence[Any]) -> list[FunctionCall[R]]:
        self._reject_import_invocation()
        return [self._spawn_dynamic(_map_args(input_value)) for input_value in inputs]

    def _spawn_dynamic(self, args: tuple[Any, ...]) -> FunctionCall[R]:
        self._ensure_invokable()
        response = self._invoke_serialized(detached=True, args=args, kwargs={})
        if not response.task_id:
            raise FunctionOperationError(response.output or "function invocation failed")
        return self._call_from_response(response)

    def _call_from_response(self, response: FunctionInvokeResponse) -> FunctionCall[R]:
        config = self._config()
        return FunctionCall(
            task_id=response.task_id,
            client=TaskClient(
                workspace=config.workspace,
                endpoint=config.endpoint,
                token=config.token,
                timeout_seconds=config.timeout_seconds,
            ),
            result_payload=response.result,
            complete=response.done,
            exit_code=response.exit_code,
            error=response.output,
            workspace_id=config.workspace,
        )

    async def async_remote(self, *args: P.args, **kwargs: P.kwargs) -> R:
        self._reject_import_invocation()
        return await to_thread(self._remote_call, *args, **kwargs)

    @overload
    async def async_spawn(self, *args: P.args, **kwargs: P.kwargs) -> FunctionCall[R]: ...

    @overload
    async def async_spawn(self, *args: Any, **kwargs: Any) -> FunctionCall[R]: ...

    async def async_spawn(self, *args: Any, **kwargs: Any) -> FunctionCall[R]:
        self._reject_import_invocation()
        return await to_thread(self.spawn, *args, **kwargs)

    def map(self, inputs: Sequence[Any]) -> Iterator[R | None]:
        calls = self.spawn_map(inputs)
        if not calls:
            return
        for call in calls:
            try:
                yield call.get(timeout_seconds=self._effective_timeout_seconds())
            except TaskOperationError as exc:
                self._error(f"Task failed during map: {exc}")
                yield None

    def _ensure_prepared(self) -> None:
        if not self.stub_id:
            self.prepare()

    @staticmethod
    def _reject_import_invocation() -> None:
        if called_on_import():
            msg = "remote function invocation is unavailable while importing user code"
            raise FunctionOperationError(msg)

    def _ensure_invokable(self) -> None:
        if self.stub_id:
            return
        if is_local():
            self._ensure_prepared()
            return
        self._resolve_deployed_stub_id()

    def _resolve_deployed_stub_id(self) -> None:
        try:
            response = DeploymentClient(
                client=self.deployment_client,
                workspace=self._config().workspace,
                endpoint=self.endpoint,
                token=self.token,
                timeout_seconds=self.timeout,
            ).resolve_target(
                kind=DeploymentKind.Function,
                name=self.resource_name,
                app=self._app_slug,
            )
        except RuntimeError as exc:
            raise FunctionOperationError(str(exc)) from exc
        if not response.stub_id:
            msg = f"deployed function target not found: {self.resource_name}"
            raise FunctionOperationError(msg)
        self.stub_id = response.stub_id

    def _invoke(
        self,
        detached: bool,
        *args: Any,
        **kwargs: Any,
    ) -> FunctionInvokeResponse:
        return self._invoke_serialized(detached=detached, args=args, kwargs=kwargs)

    def _invoke_serialized(
        self,
        *,
        detached: bool,
        args: tuple[Any, ...],
        kwargs: Mapping[str, Any],
        step: TerminalStep | None = None,
    ) -> FunctionInvokeResponse:
        if not self.stub_id:
            msg = "stub_id is required to invoke a remote function"
            raise FunctionOperationError(msg)
        last_response: FunctionInvokeResponse | None = None
        serialized = _serialize_invocation(args, kwargs)
        parent_task_id, root_task_id = _current_task_context()
        reported_task_id = ""
        try:
            for response in self.control_client.invoke(
                self.stub_id,
                serialized.payload,
                detached=detached,
                parent_task_id=parent_task_id,
                root_task_id=root_task_id,
                dependencies=serialized.dependencies,
            ):
                if response.task_id and response.task_id != reported_task_id:
                    reported_task_id = response.task_id
                    if step is not None:
                        step.update(f"{response.task_id[:8]} submitted")
                if response.status and step is not None:
                    step.update(f"{response.task_id[:8]} {response.status}")
                if response.output:
                    self._progress(
                        response.output,
                        stream="stderr" if response.exit_code else response.stream,
                    )
                last_response = response
                if response.done or response.exit_code != 0:
                    break
        except RuntimeError as exc:
            raise FunctionOperationError(str(exc)) from exc
        if last_response is None:
            msg = "function invocation returned no responses"
            raise FunctionOperationError(msg)
        return last_response

    def _progress(self, message: str, *, stream: str) -> None:
        if self.terminal is not None and message:
            self.terminal.remote_output(message, stream=stream)

    def _task_step(self) -> AbstractContextManager[TerminalStep]:
        if self.terminal is None:
            return nullcontext(TerminalStep(name="Task", terminal=Terminal(quiet=True)))
        return self.terminal.step("Task", "submitting")

    def _error(self, message: str) -> None:
        terminal = self.terminal or Terminal()
        terminal.error(message)

    def _effective_timeout_seconds(self) -> int | None:
        if self.task_policy is not None and self.task_policy.timeout_seconds is not None:
            return self.task_policy.timeout_seconds
        return self.timeout_seconds


def _map_args(input_value: Any) -> tuple[Any, ...]:
    if isinstance(input_value, InvocationIterable) and _is_invocation_sequence(input_value):
        return _invocation_tuple(input_value)
    return (input_value,)


def _serialize_invocation(
    args: tuple[Any, ...], kwargs: Mapping[str, Any]
) -> SerializedFunctionInvocation:
    dependencies: list[FunctionCallDependency] = []
    seen: set[str] = set()
    serialized_args = _function_call_ref_tuple(args, dependencies, seen)
    serialized_kwargs = _function_call_ref_mapping(dict(kwargs), dependencies, seen)
    payload: dict[str, object] = {
        "args": serialized_args,
        "kwargs": serialized_kwargs,
    }
    try:
        public_arguments = FunctionInvocationArguments.model_validate(
            {
                "args": list(serialized_args),
                "kwargs": serialized_kwargs,
            },
            strict=True,
        )
    except ValidationError:
        public_arguments = None
    return SerializedFunctionInvocation(
        payload=FunctionCloudpickleInvocation.from_bytes(
            cloudpickle_bytes(payload),
            arguments=public_arguments,
        ),
        dependencies=dependencies,
    )


def _function_call_ref_payload(
    value: Any,
    dependencies: list[FunctionCallDependency],
    seen: set[str],
) -> Any:
    if isinstance(value, FunctionCall):
        if value.task_id not in seen:
            seen.add(value.task_id)
            dependencies.append(
                FunctionCallDependency(
                    task_id=value.task_id,
                    workspace_id=value.workspace_id,
                )
            )
        return {FUNCTION_CALL_REF_MARKER: True, "task_id": value.task_id}
    if isinstance(value, InvocationMapping) and _is_invocation_mapping(value):
        return _function_call_ref_mapping(value, dependencies, seen)
    if isinstance(value, InvocationIterable):
        if _is_invocation_list(value):
            return _function_call_ref_list(value, dependencies, seen)
        if _is_invocation_tuple(value):
            return _function_call_ref_tuple(value, dependencies, seen)
    return value


def _invocation_tuple(values: InvocationIterable) -> tuple[Any, ...]:
    return tuple(values)


def _is_invocation_sequence(value: InvocationIterable) -> bool:
    return isinstance(value, tuple | list)


def _is_invocation_mapping(value: InvocationMapping) -> bool:
    return isinstance(value, dict)


def _is_invocation_list(value: InvocationIterable) -> bool:
    return isinstance(value, list)


def _is_invocation_tuple(value: InvocationIterable) -> bool:
    return isinstance(value, tuple)


def _function_call_ref_mapping(
    value: InvocationMapping,
    dependencies: list[FunctionCallDependency],
    seen: set[str],
) -> dict[Any, Any]:
    return {
        key: _function_call_ref_payload(item, dependencies, seen) for key, item in value.items()
    }


def _function_call_ref_list(
    value: InvocationIterable,
    dependencies: list[FunctionCallDependency],
    seen: set[str],
) -> list[Any]:
    return [_function_call_ref_payload(item, dependencies, seen) for item in value]


def _function_call_ref_tuple(
    value: InvocationIterable,
    dependencies: list[FunctionCallDependency],
    seen: set[str],
) -> tuple[Any, ...]:
    return tuple(_function_call_ref_payload(item, dependencies, seen) for item in value)


def _current_task_context() -> tuple[str, str]:
    parent_task_id = current_task_id()
    return parent_task_id, current_root_task_id()


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
    autoscaler: QueueDepthAutoscaler | Mapping[str, Any] | None = None,
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
    preemptible: bool = False,
    region: str | None = None,
    pool: PoolInput = None,
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
    autoscaler: QueueDepthAutoscaler | Mapping[str, Any] | None = None,
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
    preemptible: bool = False,
    region: str | None = None,
    pool: PoolInput = None,
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
    autoscaler: QueueDepthAutoscaler | Mapping[str, Any] | None = None,
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
    preemptible: bool = False,
    region: str | None = None,
    pool: PoolInput = None,
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
            pool=pool,
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


def _default_function_client(config: ControlClientConfig) -> FunctionControlClient:
    return FunctionControlClient.from_endpoint(
        config.endpoint,
        token=config.token,
        workspace=config.workspace,
        timeout_seconds=config.timeout_seconds,
    )


__all__ = [
    "Function",
    "FunctionCall",
    "FunctionOperationError",
    "FunctionOptions",
    "VolumeExport",
    "_function",
]
