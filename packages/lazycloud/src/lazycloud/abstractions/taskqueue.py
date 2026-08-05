from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from functools import update_wrapper
from pathlib import Path
from typing import Any, Generic, ParamSpec, Protocol, TypedDict, TypeVar, overload

from shared.autoscaling import QueueDepthAutoscaler
from shared.deployment_records import (
    DEFAULT_DISK,
    DEFAULT_TASK_QUEUE_CPU,
    DEFAULT_TASK_QUEUE_MEMORY,
    DeploymentSpec,
    Resources,
    VolumeMount,
)
from shared.deployments import DeploymentKind
from shared.http.gateway import DeployStubResponse
from shared.http.taskqueues import (
    DEFAULT_TASK_QUEUE_SERVE_TIMEOUT_SECONDS,
    StartTaskQueueServeResponse,
    TaskQueueInvocationEnvelope,
    TaskQueuePutResponse,
)
from shared.tasks import RetryPolicy, TaskPolicy

from lazycloud.abstractions.image import Image
from lazycloud.abstractions.invocation import (
    InvocationOptions,
    InvocationTarget,
    InvocationTargetError,
    InvocationTargetName,
    resolve_invocation_target,
)
from lazycloud.abstractions.metadata import (
    LifecycleHookInput,
    PoolInput,
    RetryPolicyInput,
    SchemaInput,
    build_resource_metadata,
    lifecycle_hooks,
    retry_policy_config,
)
from lazycloud.abstractions.serve import (
    ServeGatewayClient,
    ServePreviewSession,
    ServeResourceClient,
    resolve_serve_url,
    write_serve_preview,
)
from lazycloud.abstractions.volume import VolumeExport, volume_mounts
from lazycloud.client_contracts import (
    build_client_contract,
    schema_from_contract_parameters,
    schema_from_contract_return,
)
from lazycloud.clients.gateway.control import GatewayControlClient
from lazycloud.clients.resource.control import ResourceControlClient
from lazycloud.clients.taskqueue.control import TaskQueueControlClient
from lazycloud.control import ControlClientConfig, resolve_control_client_config
from lazycloud.env import is_local
from lazycloud.references import dotted_reference
from lazycloud.session.deployment import DeploymentClient, DeploymentControlClient
from lazycloud.session.task import Task, TaskBatch, TaskClient
from lazycloud.terminal import Terminal
from lazycloud.values import cloudpickle_bytes

P = ParamSpec("P")
R = TypeVar("R")


class TaskQueueClient(Protocol):
    def put(self, stub_id: str, payload: bytes) -> TaskQueuePutResponse: ...

    def start_serve(
        self,
        stub_id: str,
        *,
        timeout: int = DEFAULT_TASK_QUEUE_SERVE_TIMEOUT_SECONDS,
    ) -> StartTaskQueueServeResponse: ...


class TaskQueueOperationError(RuntimeError):
    pass


class TaskQueueOptions(TypedDict, total=False):
    image: Image | None
    name: str | None
    cpu: float | None
    memory: str | None
    disk: str | None
    gpu: str | None
    gpu_count: int
    timeout: int | None
    retries: int
    retry_policy: RetryPolicyInput
    retry_for: Iterable[type[BaseException]] | None
    retry_delay_seconds: float
    workers: int
    keep_warm_seconds: int
    max_pending_tasks: int
    callback_url: str
    authorized: bool
    env: dict[str, str] | None
    secrets: list[str] | None
    volumes: Iterable[VolumeMount | VolumeExport] | None
    on_start: LifecycleHookInput
    on_running: LifecycleHookInput
    on_success: LifecycleHookInput
    on_error: LifecycleHookInput
    on_retry: LifecycleHookInput
    on_failure: LifecycleHookInput
    on_cancelled: LifecycleHookInput
    on_timeout: LifecycleHookInput
    on_finish: LifecycleHookInput
    autoscaler: QueueDepthAutoscaler | Mapping[str, Any] | None
    task_policy: TaskPolicy | Mapping[str, Any] | None
    checkpoint_enabled: bool
    inputs: SchemaInput
    outputs: SchemaInput
    docker_enabled: bool
    preemptible: bool
    pool: PoolInput
    provider: str | None
    metadata: dict[str, Any] | None


class _QueuedTask(Task):
    @property
    def id(self) -> str:
        return self.task_id


@dataclass
class TaskQueueFunction(Generic[P, R]):
    func: Callable[P, R]
    _app_slug: str
    image: Image = field(default_factory=Image)
    name: str | None = None
    cpu: float | None = DEFAULT_TASK_QUEUE_CPU
    memory: str | None = DEFAULT_TASK_QUEUE_MEMORY
    disk: str | None = None
    gpu: str | None = None
    gpu_count: int = 0
    timeout: int | None = 3600
    retries: int = 3
    retry_policy: RetryPolicyInput = None
    retry_for: Iterable[type[BaseException]] = field(default_factory=tuple)
    retry_delay_seconds: float = 0.0
    workers: int = 1
    keep_warm_seconds: int = 10
    max_pending_tasks: int = 100
    callback_url: str = ""
    authorized: bool = True
    env: dict[str, str] = field(default_factory=dict)
    secrets: list[str] = field(default_factory=list)
    volumes: list[VolumeMount] = field(default_factory=list)
    on_start: LifecycleHookInput = None
    on_running: LifecycleHookInput = None
    on_success: LifecycleHookInput = None
    on_error: LifecycleHookInput = None
    on_retry: LifecycleHookInput = None
    on_failure: LifecycleHookInput = None
    on_cancelled: LifecycleHookInput = None
    on_timeout: LifecycleHookInput = None
    on_finish: LifecycleHookInput = None
    autoscaler: QueueDepthAutoscaler | Mapping[str, Any] | None = None
    task_policy: TaskPolicy | Mapping[str, Any] | None = None
    checkpoint_enabled: bool = False
    inputs: SchemaInput = None
    outputs: SchemaInput = None
    docker_enabled: bool = False
    preemptible: bool = False
    pool: PoolInput = None
    provider: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    stub_id: str = field(default="", init=False)
    client: TaskQueueClient | None = field(default=None, init=False, repr=False)
    deployment_client: DeploymentControlClient | None = field(
        default=None,
        init=False,
        repr=False,
    )
    endpoint: str | None = field(default=None, init=False)
    token: str | None = field(default=None, init=False, repr=False)
    client_timeout_seconds: float = field(default=10.0, init=False)
    terminal: Terminal | None = field(default=None, init=False, repr=False)
    sync_local_dir: str | None = field(default=None, init=False)
    gateway_client: ServeGatewayClient | None = field(default=None, init=False, repr=False)
    resource_client: ServeResourceClient | None = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        _ = self.effective_retry_policy
        update_wrapper(self, self.func)

    @property
    def resource_name(self) -> str:
        return self.name or self.func.__name__

    @property
    def effective_retry_policy(self) -> RetryPolicy | None:
        return retry_policy_config(
            self.retry_policy,
            retries=self.retries,
            retry_delay_seconds=self.retry_delay_seconds,
            retry_for_refs=_retry_for_references(self.retry_for),
        )

    @property
    def control_client(self) -> TaskQueueClient:
        if self.client is None:
            self.client = _default_task_queue_client(self._config())
        return self.client

    def __call__(self, *args: P.args, **kwargs: P.kwargs) -> R:
        if not is_local():
            return self.local(*args, **kwargs)
        msg = (
            "Direct calls to TaskQueues are not supported. "
            "To enqueue items use .put(*args, **kwargs)"
        )
        raise NotImplementedError(msg)

    def local(self, *args: P.args, **kwargs: P.kwargs) -> R:
        return self.func(*args, **kwargs)

    def spec(self) -> DeploymentSpec:
        client_contract = build_client_contract(
            self.func,
            kind=DeploymentKind.TaskQueue,
            inputs=self.inputs,
            outputs=self.outputs,
        )
        return DeploymentSpec(
            name=self.resource_name,
            kind=DeploymentKind.TaskQueue,
            handler=dotted_reference(self.func),
            image=self.image.spec(),
            resources=Resources(
                cpu=self.cpu,
                memory=self.memory,
                disk=self.disk or DEFAULT_DISK,
                gpu=self.gpu,
                gpu_count=self.gpu_count,
                timeout_seconds=self.timeout,
                concurrency=self.workers,
                keep_warm=self.keep_warm_seconds,
                preemptible=self.preemptible,
            ),
            env=self.env,
            secrets=self.secrets,
            volumes=list(self.volumes),
            retry_policy=self.effective_retry_policy,
            lifecycle_hooks=lifecycle_hooks(
                on_start=self.on_start,
                on_running=self.on_running,
                on_success=self.on_success,
                on_error=self.on_error,
                on_retry=self.on_retry,
                on_failure=self.on_failure,
                on_cancelled=self.on_cancelled,
                on_timeout=self.on_timeout,
                on_finish=self.on_finish,
            ),
            metadata=build_resource_metadata(
                app=self._app_slug,
                workers=self.workers,
                max_pending_tasks=self.max_pending_tasks,
                retries=self.retries,
                callback_url=self.callback_url,
                authorized=self.authorized,
                autoscaler=self.autoscaler,
                task_policy=self.task_policy,
                checkpoint_enabled=self.checkpoint_enabled,
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
                provider=self.provider,
                extra=self.metadata,
            ),
            client_contract=client_contract,
        )

    def prepare(
        self,
        *,
        workspace: str | None = None,
        source_root: str | None = None,
    ) -> str:
        try:
            response = DeploymentClient(
                client=self.deployment_client,
                workspace=workspace,
                endpoint=self.endpoint,
                token=self.token,
                timeout_seconds=self.client_timeout_seconds,
                sync_source=True,
                terminal=self.terminal,
            ).prepare(
                self.spec(),
                workspace=workspace,
                image=self.image,
                source_root=source_root,
            )
        except RuntimeError as exc:
            raise TaskQueueOperationError(str(exc)) from exc
        if not response.stub_id:
            msg = "deployment prepare did not return a task queue stub_id"
            raise TaskQueueOperationError(msg)
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
                timeout_seconds=self.client_timeout_seconds,
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
            raise TaskQueueOperationError(str(exc)) from exc
        self.stub_id = response.stub_id or self.stub_id
        return response

    def put(
        self,
        *args: P.args,
        **kwargs: P.kwargs,
    ) -> _QueuedTask:
        return self._put_with_options(InvocationOptions(), args=args, kwargs=kwargs)

    def target(
        self,
        target: InvocationTargetName = "auto",
        *,
        deployment_name: str | None = None,
        deployment_version: int | None = None,
    ) -> TaskQueueInvocation[P, R]:
        return TaskQueueInvocation(
            owner=self,
            options=InvocationOptions(
                target=target,
                deployment_name=deployment_name,
                deployment_version=deployment_version,
            ),
        )

    def _put_with_options(
        self,
        options: InvocationOptions,
        *,
        args: tuple[Any, ...],
        kwargs: Mapping[str, Any],
    ) -> _QueuedTask:
        stub_id = self._target_stub_id(options)
        call_kwargs = dict(kwargs)
        return self._put_task(stub_id, args=args, kwargs=call_kwargs)

    def put_many(
        self,
        items: Iterable[Any],
        **shared_kwargs: Any,
    ) -> TaskBatch:
        return self._put_many_with_options(
            InvocationOptions(),
            items=items,
            shared_kwargs=shared_kwargs,
        )

    def _put_many_with_options(
        self,
        options: InvocationOptions,
        *,
        items: Iterable[Any],
        shared_kwargs: Mapping[str, Any],
    ) -> TaskBatch:
        queued_items = tuple(items)
        if not queued_items:
            return TaskBatch(())
        stub_id = self._target_stub_id(options)
        call_kwargs = dict(shared_kwargs)
        handles: list[Task] = []
        for index, item in enumerate(queued_items):
            try:
                handles.append(self._put_task(stub_id, args=(item,), kwargs=call_kwargs))
            except TaskQueueOperationError as exc:
                msg = f"failed to enqueue task queue item {index}: {exc}"
                raise TaskQueueOperationError(msg) from exc
        return TaskBatch(tuple(handles))

    def _put_task(
        self,
        stub_id: str,
        *,
        args: tuple[Any, ...],
        kwargs: Mapping[str, Any],
    ) -> _QueuedTask:
        try:
            response = self.control_client.put(
                stub_id,
                cloudpickle_bytes(TaskQueueInvocationEnvelope(args=args, kwargs=dict(kwargs))),
            )
        except RuntimeError as exc:
            raise TaskQueueOperationError(str(exc)) from exc
        if not response.task_id:
            msg = "task queue enqueue did not return a task_id"
            raise TaskQueueOperationError(msg)
        return _QueuedTask(
            task_id=response.task_id,
            client=self._task_client(),
        )

    def _target_stub_id(self, options: InvocationOptions) -> str:
        bound_stub_default_target = (
            self.stub_id
            and options.target == "auto"
            and options.deployment_name is None
            and options.deployment_version is None
        )
        if bound_stub_default_target:
            return self.stub_id
        return self._resolve_invocation_target(options).stub_id

    def _resolve_invocation_target(
        self,
        options: InvocationOptions,
    ) -> InvocationTarget:
        config = self._config()
        preview_client = self.resource_client or ResourceControlClient.from_endpoint(
            config.endpoint,
            token=config.token,
            timeout_seconds=config.timeout_seconds,
            workspace=config.workspace,
        )
        try:
            return resolve_invocation_target(
                kind=DeploymentKind.TaskQueue,
                name=self.resource_name,
                app=self._app_slug,
                config=config,
                deployment_client=self.deployment_client,
                preview_client=preview_client,
                target=options.target,
                deployment_name=options.deployment_name,
                deployment_version=options.deployment_version,
            )
        except InvocationTargetError as exc:
            raise TaskQueueOperationError(str(exc)) from exc

    def serve(
        self,
        timeout: int = 0,
        url_type: str = "",
    ) -> StartTaskQueueServeResponse:
        terminal = self.terminal or Terminal()
        self.terminal = terminal
        selected_sync_dir = self.sync_local_dir if self.sync_local_dir is not None else "."
        serve_timeout = timeout if timeout > 0 else DEFAULT_TASK_QUEUE_SERVE_TIMEOUT_SECONDS
        stub_id = self.stub_id or self._prepare_serve_preview(
            serve_timeout=serve_timeout,
            source_root=selected_sync_dir or None,
        )
        config = self._config()
        gateway_client = self.gateway_client or GatewayControlClient.from_endpoint(
            config.endpoint,
            token=config.token,
            timeout_seconds=config.timeout_seconds,
        )
        resource_client = self.resource_client or ResourceControlClient.from_endpoint(
            config.endpoint,
            token=config.token,
            timeout_seconds=config.timeout_seconds,
            workspace=config.workspace,
        )
        try:
            serve_url = resolve_serve_url(
                gateway_client,
                stub_id=stub_id,
                url_type=url_type,
                workspace=None,
                external_url=config.endpoint,
            )
        except RuntimeError as exc:
            raise TaskQueueOperationError(str(exc)) from exc
        try:
            response = self.control_client.start_serve(stub_id, timeout=serve_timeout)
        except RuntimeError as exc:
            raise TaskQueueOperationError(str(exc) or "failed to serve task queue") from exc
        if not response.container_id:
            raise TaskQueueOperationError("failed to serve task queue")
        preview_record = write_serve_preview(
            kind=DeploymentKind.TaskQueue,
            name=self.resource_name,
            app=self._app_slug,
            workspace=config.workspace,
            endpoint=config.endpoint,
            stub_id=stub_id,
            container_id=response.container_id,
            url=serve_url.url,
        )
        ServePreviewSession(
            stub_id=stub_id,
            container_id=response.container_id,
            url=serve_url.url,
            gateway_client=gateway_client,
            resource_client=resource_client,
            terminal=terminal,
            sync_dir=selected_sync_dir,
            token=self.token,
            authorized=self.authorized,
            preview_record=preview_record,
        ).run()
        return response

    def _prepare_serve_preview(self, *, serve_timeout: int, source_root: str | None) -> str:
        original_keep_warm = self.keep_warm_seconds
        self.keep_warm_seconds = max(original_keep_warm, serve_timeout)
        try:
            return self.prepare(source_root=source_root)
        finally:
            self.keep_warm_seconds = original_keep_warm

    def _config(self) -> ControlClientConfig:
        return resolve_control_client_config(
            endpoint=self.endpoint,
            token=self.token,
            timeout_seconds=self.client_timeout_seconds,
        )

    def _task_client(self) -> TaskClient:
        config = self._config()
        return TaskClient(
            workspace=config.workspace,
            endpoint=config.endpoint,
            token=config.token,
            timeout_seconds=config.timeout_seconds,
        )


@dataclass(frozen=True)
class TaskQueueInvocation(Generic[P, R]):
    owner: TaskQueueFunction[P, R]
    options: InvocationOptions

    def put(self, *args: P.args, **kwargs: P.kwargs) -> _QueuedTask:
        return self.owner._put_with_options(self.options, args=args, kwargs=kwargs)

    def put_many(self, items: Iterable[Any], **shared_kwargs: Any) -> TaskBatch:
        return self.owner._put_many_with_options(
            self.options,
            items=items,
            shared_kwargs=shared_kwargs,
        )


@overload
def _task_queue(
    func: Callable[P, R],
    *,
    _app_slug: str,
    image: Image | None = None,
    name: str | None = None,
    cpu: float | None = DEFAULT_TASK_QUEUE_CPU,
    memory: str | None = DEFAULT_TASK_QUEUE_MEMORY,
    disk: str | None = None,
    gpu: str | None = None,
    gpu_count: int = 0,
    timeout: int | None = 3600,
    retries: int = 3,
    retry_for: Iterable[type[BaseException]] | None = None,
    retry_delay_seconds: float = 0.0,
    retry_policy: RetryPolicy | Mapping[str, Any] | None = None,
    workers: int = 1,
    keep_warm_seconds: int = 10,
    max_pending_tasks: int = 100,
    callback_url: str = "",
    authorized: bool = True,
    env: dict[str, str] | None = None,
    secrets: list[str] | None = None,
    volumes: Iterable[VolumeMount | VolumeExport] | None = None,
    on_start: LifecycleHookInput = None,
    on_running: LifecycleHookInput = None,
    on_success: LifecycleHookInput = None,
    on_error: LifecycleHookInput = None,
    on_retry: LifecycleHookInput = None,
    on_failure: LifecycleHookInput = None,
    on_cancelled: LifecycleHookInput = None,
    on_timeout: LifecycleHookInput = None,
    on_finish: LifecycleHookInput = None,
    autoscaler: QueueDepthAutoscaler | Mapping[str, Any] | None = None,
    task_policy: TaskPolicy | Mapping[str, Any] | None = None,
    checkpoint_enabled: bool = False,
    inputs: SchemaInput = None,
    outputs: SchemaInput = None,
    docker_enabled: bool = False,
    preemptible: bool = False,
    pool: PoolInput = None,
    provider: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> TaskQueueFunction[P, R]: ...


@overload
def _task_queue(
    func: None = None,
    *,
    _app_slug: str,
    image: Image | None = None,
    name: str | None = None,
    cpu: float | None = DEFAULT_TASK_QUEUE_CPU,
    memory: str | None = DEFAULT_TASK_QUEUE_MEMORY,
    disk: str | None = None,
    gpu: str | None = None,
    gpu_count: int = 0,
    timeout: int | None = 3600,
    retries: int = 3,
    retry_for: Iterable[type[BaseException]] | None = None,
    retry_delay_seconds: float = 0.0,
    retry_policy: RetryPolicy | Mapping[str, Any] | None = None,
    workers: int = 1,
    keep_warm_seconds: int = 10,
    max_pending_tasks: int = 100,
    callback_url: str = "",
    authorized: bool = True,
    env: dict[str, str] | None = None,
    secrets: list[str] | None = None,
    volumes: Iterable[VolumeMount | VolumeExport] | None = None,
    on_start: LifecycleHookInput = None,
    on_running: LifecycleHookInput = None,
    on_success: LifecycleHookInput = None,
    on_error: LifecycleHookInput = None,
    on_retry: LifecycleHookInput = None,
    on_failure: LifecycleHookInput = None,
    on_cancelled: LifecycleHookInput = None,
    on_timeout: LifecycleHookInput = None,
    on_finish: LifecycleHookInput = None,
    autoscaler: QueueDepthAutoscaler | Mapping[str, Any] | None = None,
    task_policy: TaskPolicy | Mapping[str, Any] | None = None,
    checkpoint_enabled: bool = False,
    inputs: SchemaInput = None,
    outputs: SchemaInput = None,
    docker_enabled: bool = False,
    preemptible: bool = False,
    pool: PoolInput = None,
    provider: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> Callable[[Callable[P, R]], TaskQueueFunction[P, R]]: ...


def _task_queue(
    func: Callable[P, R] | None = None,
    *,
    _app_slug: str,
    image: Image | None = None,
    name: str | None = None,
    cpu: float | None = DEFAULT_TASK_QUEUE_CPU,
    memory: str | None = DEFAULT_TASK_QUEUE_MEMORY,
    disk: str | None = None,
    gpu: str | None = None,
    gpu_count: int = 0,
    timeout: int | None = 3600,
    retries: int = 3,
    retry_for: Iterable[type[BaseException]] | None = None,
    retry_delay_seconds: float = 0.0,
    retry_policy: RetryPolicy | Mapping[str, Any] | None = None,
    workers: int = 1,
    keep_warm_seconds: int = 10,
    max_pending_tasks: int = 100,
    callback_url: str = "",
    authorized: bool = True,
    env: dict[str, str] | None = None,
    secrets: list[str] | None = None,
    volumes: Iterable[VolumeMount | VolumeExport] | None = None,
    on_start: LifecycleHookInput = None,
    on_running: LifecycleHookInput = None,
    on_success: LifecycleHookInput = None,
    on_error: LifecycleHookInput = None,
    on_retry: LifecycleHookInput = None,
    on_failure: LifecycleHookInput = None,
    on_cancelled: LifecycleHookInput = None,
    on_timeout: LifecycleHookInput = None,
    on_finish: LifecycleHookInput = None,
    autoscaler: QueueDepthAutoscaler | Mapping[str, Any] | None = None,
    task_policy: TaskPolicy | Mapping[str, Any] | None = None,
    checkpoint_enabled: bool = False,
    inputs: SchemaInput = None,
    outputs: SchemaInput = None,
    docker_enabled: bool = False,
    preemptible: bool = False,
    pool: PoolInput = None,
    provider: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> Callable[[Callable[P, R]], TaskQueueFunction[P, R]] | TaskQueueFunction[P, R]:
    def decorate(target: Callable[P, R]) -> TaskQueueFunction[P, R]:
        return TaskQueueFunction(
            target,
            _app_slug=_app_slug,
            image=image or Image(),
            name=name,
            cpu=cpu,
            memory=memory,
            disk=disk,
            gpu=gpu,
            gpu_count=gpu_count,
            timeout=timeout,
            retries=retries,
            retry_policy=retry_policy,
            retry_for=retry_for or (),
            retry_delay_seconds=retry_delay_seconds,
            workers=workers,
            keep_warm_seconds=keep_warm_seconds,
            max_pending_tasks=max_pending_tasks,
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
            on_cancelled=on_cancelled,
            on_timeout=on_timeout,
            on_finish=on_finish,
            autoscaler=autoscaler,
            task_policy=task_policy,
            checkpoint_enabled=checkpoint_enabled,
            inputs=inputs,
            outputs=outputs,
            docker_enabled=docker_enabled,
            preemptible=preemptible,
            pool=pool,
            provider=provider,
            metadata=metadata or {},
        )

    if func is None:
        return decorate
    return decorate(func)


def _retry_for_references(retry_for: Iterable[type[BaseException]]) -> list[str]:
    references: list[str] = []
    for exception_type in tuple(retry_for):
        if not isinstance(exception_type, type) or not issubclass(
            exception_type,
            BaseException,
        ):
            msg = "retry_for entries must be exception classes"
            raise TypeError(msg)
        references.append(dotted_reference(exception_type))
    return references


def _default_task_queue_client(config: ControlClientConfig) -> TaskQueueControlClient:
    return TaskQueueControlClient.from_endpoint(
        config.endpoint,
        token=config.token,
        timeout_seconds=config.timeout_seconds,
    )


__all__ = [
    "TaskQueueClient",
    "TaskQueueFunction",
    "TaskQueueInvocation",
    "TaskQueueOperationError",
    "TaskQueueOptions",
    "VolumeExport",
    "_task_queue",
]
