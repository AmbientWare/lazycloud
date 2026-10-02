from __future__ import annotations

import inspect
import json
import types
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from functools import update_wrapper
from pathlib import Path
from typing import (
    TYPE_CHECKING,
    Any,
    Generic,
    ParamSpec,
    Protocol,
    TypeAlias,
    TypedDict,
    TypeVar,
    overload,
    runtime_checkable,
)

from pydantic import JsonValue
from shared.autoscaling import Autoscaler
from shared.deployment_records import (
    DEFAULT_DISK,
    DEFAULT_HTTP_CPU,
    DEFAULT_HTTP_MEMORY,
    DEFAULT_WORKLOAD_PREEMPTIBLE,
    CpuRequest,
    DeploymentSpec,
    MemoryRequest,
    Resources,
    VolumeMount,
)
from shared.deployments import DEFAULT_ENDPOINT_METHODS, DeploymentKind
from shared.function_payloads import FunctionPayloadEncoding
from shared.gpu import GpuInput, gpu_preference
from shared.placement import ProductRegion
from shared.serialization import to_json_value
from shared.tasks import RetryPolicy, TaskPolicy

from lazycloud._invocation import encode_arguments, prepare_arguments, serialize_result
from lazycloud.abstractions.function import FunctionOperationError
from lazycloud.abstractions.http_calls import (
    EndpointResponse,
    InvocationOptions,
    InvocationTargetName,
    http_workload_spec,
    resolve_url,
    send_request,
    unsupported_http_options,
)
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
from lazycloud.client_contracts import (
    asgi_client_contract,
    build_client_contract,
    schema_from_contract_parameters,
    schema_from_contract_return,
)
from lazycloud.env import is_local
from lazycloud.exceptions import UnsupportedFeatureError
from lazycloud.references import dotted_reference
from lazycloud.terminal import Terminal

if TYPE_CHECKING:
    from shared.api import Deployment, Preview, WorkloadSpec

    from lazycloud.abstractions.image import ImageBuildResult
    from lazycloud.abstractions.shell import ShellSession

P = ParamSpec("P")
R = TypeVar("R")

ASGIMessage: TypeAlias = dict[str, Any]
ASGIReceive: TypeAlias = Callable[[], Awaitable[ASGIMessage]]
ASGISend: TypeAlias = Callable[[ASGIMessage], Awaitable[None]]


@runtime_checkable
class RealtimeAsyncIterable(Protocol):
    def __aiter__(self) -> AsyncIterator[Any]: ...


@runtime_checkable
class RealtimeIterable(Protocol):
    def __iter__(self) -> Iterator[Any]: ...


ENDPOINT_DIRECT_CALL_ERROR = (
    "direct calls to endpoints are not supported outside worker containers; "
    "use .local(...) for local execution"
)


class EndpointOperationError(FunctionOperationError):
    pass


class EndpointOptions(TypedDict, total=False):
    image: Image | None
    name: str | None
    route: str
    domain: str | None
    methods: list[str] | None
    cpu: CpuRequest | None
    memory: MemoryRequest | None
    disk: str | None
    gpu: GpuInput
    gpu_count: int
    timeout_seconds: int | None
    retries: int
    retry_policy: RetryPolicyInput
    retry_delay_seconds: float
    workers: int
    concurrency: int
    keep_warm: int
    max_pending_tasks: int | None
    callback_url: str | None
    authorized: bool | None
    env: dict[str, str] | None
    secrets: list[str] | None
    volumes: Iterable[VolumeMount | VolumeExport] | None
    on_start: LifecycleHookInput
    autoscaler: Autoscaler | Mapping[str, Any] | None
    task_policy: TaskPolicy | Mapping[str, Any] | None
    checkpoint_enabled: bool
    inputs: SchemaInput
    outputs: SchemaInput
    docker_enabled: bool
    preemptible: bool
    region: str | None
    availability_zone: str
    machine: MachineInput
    metadata: dict[str, Any] | None


class ASGIOptions(TypedDict, total=False):
    name: str
    image: Image | None
    route: str
    domain: str | None
    cpu: CpuRequest | None
    memory: MemoryRequest | None
    disk: str | None
    gpu: GpuInput
    gpu_count: int
    timeout_seconds: int | None
    workers: int
    concurrent_requests: int
    keep_warm_seconds: int
    max_pending_tasks: int
    authorized: bool
    callback_url: str | None
    env: dict[str, str] | None
    secrets: list[str] | None
    volumes: Iterable[VolumeMount | VolumeExport] | None
    on_start: LifecycleHookInput
    autoscaler: Autoscaler | Mapping[str, Any] | None
    task_policy: TaskPolicy | Mapping[str, Any] | None
    checkpoint_enabled: bool
    preemptible: bool
    region: str | None
    availability_zone: str
    machine: MachineInput


@dataclass
class Endpoint(Generic[P, R]):
    func: Callable[P, R]
    _app_slug: str
    image: Image = field(default_factory=Image)
    name: str | None = None
    route: str = "/"
    domain: str | None = None
    methods: list[str] = field(default_factory=lambda: list(DEFAULT_ENDPOINT_METHODS))
    cpu: CpuRequest | None = DEFAULT_HTTP_CPU
    memory: MemoryRequest | None = DEFAULT_HTTP_MEMORY
    disk: str | None = None
    gpu: GpuInput = None
    gpu_count: int = 0
    timeout_seconds: int | None = 180
    retries: int = 0
    retry_policy: RetryPolicyInput = None
    retry_delay_seconds: float = 0.0
    workers: int = 1
    concurrency: int = 1
    keep_warm: int | None = 180
    max_pending_tasks: int | None = 100
    callback_url: str | None = None
    authorized: bool | None = True
    env: dict[str, str] = field(default_factory=dict)
    secrets: list[str] = field(default_factory=list)
    volumes: list[VolumeMount] = field(default_factory=list)
    on_start: LifecycleHookInput = None
    autoscaler: Autoscaler | Mapping[str, Any] | None = None
    task_policy: TaskPolicy | Mapping[str, Any] | None = None
    checkpoint_enabled: bool = False
    inputs: SchemaInput = None
    outputs: SchemaInput = None
    docker_enabled: bool = False
    preemptible: bool = DEFAULT_WORKLOAD_PREEMPTIBLE
    region: str | None = None
    availability_zone: str = ""
    machine: MachineInput = None
    metadata: dict[str, Any] = field(default_factory=dict)
    terminal: Terminal | None = field(default=None, init=False, repr=False)
    _handler_reference_override: str | None = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        update_wrapper(self, self.func)

    @property
    def resource_name(self) -> str:
        return self.name or self.func.__name__

    def __call__(self, *args: P.args, **kwargs: P.kwargs) -> R:
        if not is_local():
            return self.local(*args, **kwargs)
        raise EndpointOperationError(ENDPOINT_DIRECT_CALL_ERROR)

    def local(self, *args: P.args, **kwargs: P.kwargs) -> R:
        if self.inputs is None:
            return serialize_result(self.func, self.func(*args, **kwargs), self.outputs)
        return self.invoke_arguments(args, kwargs)

    def invoke_arguments(
        self,
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
        *,
        encoding: FunctionPayloadEncoding = FunctionPayloadEncoding.Json,
    ) -> R:
        prepared_args, prepared_kwargs = prepare_arguments(
            self.func, args, kwargs, self.inputs, encoding=encoding
        )
        return serialize_result(
            self.func, self.func(*prepared_args, **prepared_kwargs), self.outputs
        )

    def spec(self, *, kind: DeploymentKind = DeploymentKind.Endpoint) -> DeploymentSpec:
        client_contract = build_client_contract(
            self.func,
            kind=kind,
            inputs=self.inputs,
            outputs=self.outputs,
        )
        spec = DeploymentSpec(
            name=self.resource_name,
            kind=kind,
            handler=self._handler_reference(),
            image=self.image.spec(),
            resources=Resources(
                region=ProductRegion(self.region) if self.region is not None else None,
                availability_zone=self.availability_zone,
                cpu=self.cpu,
                memory=self.memory,
                disk=self.disk or DEFAULT_DISK,
                gpu=list(gpu_preference(self.gpu)),
                gpu_count=self.gpu_count,
                timeout_seconds=_effective_timeout_seconds(self.task_policy, self.timeout_seconds),
                concurrency=self.concurrency,
                keep_warm=self.keep_warm,
                preemptible=self.preemptible,
            ),
            route=self.route,
            domain=self.domain,
            methods=list(self.methods),
            env=self.env,
            secrets=self.secrets,
            volumes=list(self.volumes),
            retry_policy=retry_policy_config(
                self.retry_policy,
                retries=self.retries,
                retry_delay_seconds=self.retry_delay_seconds,
            ),
            lifecycle_hooks=lifecycle_hooks(on_start=self.on_start),
            metadata=build_resource_metadata(
                app=self._app_slug,
                workers=self.workers,
                max_pending_tasks=self.max_pending_tasks,
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
                machine=self.machine,
                extra=self.metadata,
            ),
            client_contract=client_contract,
        )
        return spec

    def effective_timeout_seconds(self) -> int | None:
        return _effective_timeout_seconds(self.task_policy, self.timeout_seconds)

    def unsupported_options(self) -> list[str]:
        """Declared options the platform cannot run yet, by name."""
        found = unsupported_http_options(self)
        if self.inputs is not None or self.outputs is not None:
            found.append("inputs/outputs schemas")
        return found

    def require_supported(self) -> None:
        unsupported = self.unsupported_options()
        if unsupported:
            raise UnsupportedFeatureError(f"endpoint {self.resource_name}", unsupported)

    def handler_reference(self) -> str:
        """The `module:qualname` the runner imports."""
        return self._handler_reference()

    def workload_spec(
        self, *, handler: str, source_sha256: str, image: ImageBuildResult
    ) -> WorkloadSpec:
        """The API definition of this endpoint for an uploaded source and a ready image."""
        self.require_supported()
        policy = retry_policy_config(
            self.retry_policy, retries=self.retries, retry_delay_seconds=self.retry_delay_seconds
        )
        return http_workload_spec(
            self,
            kind="endpoint",
            handler=handler,
            source_sha256=source_sha256,
            image=image,
            concurrency=self.concurrency,
            keep_warm=self.keep_warm,
            max_pending_tasks=self.max_pending_tasks,
            retry_policy=policy or RetryPolicy(max_attempts=1),
            route=self.route,
            methods=list(self.methods),
        )

    def deploy(
        self, *, workspace: str | None = None, source_root: str | Path | None = None
    ) -> Deployment:
        """Deploy this endpoint into its app without touching the app's other workloads."""
        return _deploy_http(self, workspace=workspace, source_root=source_root)

    def serve(self, timeout: int = 0, *, sync_dir: str | None = None) -> Preview:
        """Run a preview container that follows the working tree until Ctrl+C."""
        from lazycloud.abstractions.serve import serve_workload

        return serve_workload(
            self,
            kind="endpoint",
            authorized=self.authorized is not False,
            timeout=timeout,
            sync_dir=sync_dir or ".",
        )

    def request(self, *args: P.args, **kwargs: P.kwargs) -> EndpointResponse:
        """Call the running preview or the deployment with these arguments as JSON."""
        return self.target().request(*args, **kwargs)

    def target(
        self,
        target: InvocationTargetName = "auto",
        *,
        deployment_name: str | None = None,
        deployment_version: int | None = None,
    ) -> EndpointInvocation[P, R]:
        """Choose where `request` goes: a preview, the deployment, or a version."""
        return EndpointInvocation(
            owner=self,
            options=InvocationOptions(
                target=target,
                deployment_name=deployment_name,
                deployment_version=deployment_version,
            ),
        )

    def shell(
        self,
        *,
        workspace: str | None = None,
        container_id: str | None = None,
        sync_dir: str | None = None,
    ) -> ShellSession:
        """Open a shell container of this endpoint's working-tree release, or `container_id`."""
        return _shell_http(self, workspace=workspace, container_id=container_id, sync_dir=sync_dir)

    def set_handler(self, handler: str) -> None:
        self._handler_reference_override = handler

    def _handler_reference(self) -> str:
        return self._handler_reference_override or dotted_reference(self.func)


@dataclass(frozen=True)
class EndpointInvocation(Generic[P, R]):
    owner: Endpoint[P, R]
    options: InvocationOptions

    def request(self, *args: P.args, **kwargs: P.kwargs) -> EndpointResponse:
        owner = self.owner
        url, token, timeout = resolve_url(owner, kind="endpoint", options=self.options)
        encoded_args, encoded_kwargs = encode_arguments(owner.func, args, kwargs, owner.inputs)
        payload: dict[str, JsonValue] = {
            "args": [to_json_value(arg) for arg in encoded_args],
            "kwargs": {key: to_json_value(value) for key, value in encoded_kwargs.items()},
        }
        return send_request(
            url,
            method=_request_method(owner.methods),
            json_body=payload,
            token=token,
            timeout_seconds=timeout,
        )


def _request_method(methods: list[str]) -> str:
    selected = [method.strip().upper() for method in methods if method.strip()]
    if "POST" in selected:
        return "POST"
    return selected[0] if selected else "POST"


def _deploy_http(
    owner: Endpoint[..., Any] | ASGI, *, workspace: str | None, source_root: str | Path | None
) -> Deployment:
    from lazycloud.control import api_client, require_workspace, resolve_control_client_config
    from lazycloud.session.deployment import AppFunctions, deploy_functions

    config = resolve_control_client_config(workspace=workspace, timeout_seconds=60)
    return deploy_functions(
        [AppFunctions(app=owner._app_slug, functions=(owner,))],  # type: ignore[arg-type]
        client=api_client(config),
        workspace=require_workspace(config),
        source_root=source_root,
        terminal=owner.terminal,
    )[0]


def _shell_http(
    owner: Endpoint[..., Any] | ASGI,
    *,
    workspace: str | None,
    container_id: str | None,
    sync_dir: str | None,
) -> ShellSession:
    from lazycloud.abstractions.shell import Shell
    from lazycloud.control import api_client, require_workspace, resolve_control_client_config
    from lazycloud.session.deployment import prepare_release

    config = resolve_control_client_config(workspace=workspace, timeout_seconds=60)
    shell = Shell(workspace=workspace)
    if container_id:
        return shell.create_existing(container_id, sync_dir=sync_dir)
    release = prepare_release(
        owner,  # type: ignore[arg-type]
        client=api_client(config),
        workspace=require_workspace(config),
        terminal=owner.terminal,
    )
    return shell.create_standalone(str(release.id), sync_dir=sync_dir)


@overload
def _endpoint(
    func: Callable[P, R],
    *,
    _app_slug: str,
    image: Image | None = None,
    name: str | None = None,
    route: str = "/",
    domain: str | None = None,
    methods: list[str] | None = None,
    cpu: CpuRequest | None = DEFAULT_HTTP_CPU,
    memory: MemoryRequest | None = DEFAULT_HTTP_MEMORY,
    disk: str | None = None,
    gpu: GpuInput = None,
    gpu_count: int = 0,
    timeout_seconds: int | None = 180,
    retries: int = 0,
    retry_policy: RetryPolicy | Mapping[str, Any] | None = None,
    retry_delay_seconds: float = 0.0,
    workers: int = 1,
    concurrency: int = 1,
    keep_warm: int = 180,
    max_pending_tasks: int | None = 100,
    callback_url: str | None = None,
    authorized: bool | None = True,
    env: dict[str, str] | None = None,
    secrets: list[str] | None = None,
    volumes: Iterable[VolumeMount | VolumeExport] | None = None,
    on_start: LifecycleHookInput = None,
    autoscaler: Autoscaler | Mapping[str, Any] | None = None,
    task_policy: TaskPolicy | Mapping[str, Any] | None = None,
    checkpoint_enabled: bool = False,
    inputs: SchemaInput = None,
    outputs: SchemaInput = None,
    docker_enabled: bool = False,
    preemptible: bool = DEFAULT_WORKLOAD_PREEMPTIBLE,
    region: str | None = None,
    availability_zone: str = "",
    machine: MachineInput = None,
    metadata: dict[str, Any] | None = None,
) -> Endpoint[P, R]: ...


@overload
def _endpoint(
    func: None = None,
    *,
    _app_slug: str,
    image: Image | None = None,
    name: str | None = None,
    route: str = "/",
    domain: str | None = None,
    methods: list[str] | None = None,
    cpu: CpuRequest | None = DEFAULT_HTTP_CPU,
    memory: MemoryRequest | None = DEFAULT_HTTP_MEMORY,
    disk: str | None = None,
    gpu: GpuInput = None,
    gpu_count: int = 0,
    timeout_seconds: int | None = 180,
    retries: int = 0,
    retry_policy: RetryPolicy | Mapping[str, Any] | None = None,
    retry_delay_seconds: float = 0.0,
    workers: int = 1,
    concurrency: int = 1,
    keep_warm: int = 180,
    max_pending_tasks: int | None = 100,
    callback_url: str | None = None,
    authorized: bool | None = True,
    env: dict[str, str] | None = None,
    secrets: list[str] | None = None,
    volumes: Iterable[VolumeMount | VolumeExport] | None = None,
    on_start: LifecycleHookInput = None,
    autoscaler: Autoscaler | Mapping[str, Any] | None = None,
    task_policy: TaskPolicy | Mapping[str, Any] | None = None,
    checkpoint_enabled: bool = False,
    inputs: SchemaInput = None,
    outputs: SchemaInput = None,
    docker_enabled: bool = False,
    preemptible: bool = DEFAULT_WORKLOAD_PREEMPTIBLE,
    region: str | None = None,
    availability_zone: str = "",
    machine: MachineInput = None,
    metadata: dict[str, Any] | None = None,
) -> Callable[[Callable[P, R]], Endpoint[P, R]]: ...


def _endpoint(
    func: Callable[P, R] | None = None,
    *,
    _app_slug: str,
    image: Image | None = None,
    name: str | None = None,
    route: str = "/",
    domain: str | None = None,
    methods: list[str] | None = None,
    cpu: CpuRequest | None = DEFAULT_HTTP_CPU,
    memory: MemoryRequest | None = DEFAULT_HTTP_MEMORY,
    disk: str | None = None,
    gpu: GpuInput = None,
    gpu_count: int = 0,
    timeout_seconds: int | None = 180,
    retries: int = 0,
    retry_policy: RetryPolicy | Mapping[str, Any] | None = None,
    retry_delay_seconds: float = 0.0,
    workers: int = 1,
    concurrency: int = 1,
    keep_warm: int = 180,
    max_pending_tasks: int | None = 100,
    callback_url: str | None = None,
    authorized: bool | None = True,
    env: dict[str, str] | None = None,
    secrets: list[str] | None = None,
    volumes: Iterable[VolumeMount | VolumeExport] | None = None,
    on_start: LifecycleHookInput = None,
    autoscaler: Autoscaler | Mapping[str, Any] | None = None,
    task_policy: TaskPolicy | Mapping[str, Any] | None = None,
    checkpoint_enabled: bool = False,
    inputs: SchemaInput = None,
    outputs: SchemaInput = None,
    docker_enabled: bool = False,
    preemptible: bool = DEFAULT_WORKLOAD_PREEMPTIBLE,
    region: str | None = None,
    availability_zone: str = "",
    machine: MachineInput = None,
    metadata: dict[str, Any] | None = None,
) -> Callable[[Callable[P, R]], Endpoint[P, R]] | Endpoint[P, R]:
    def decorate(target: Callable[P, R]) -> Endpoint[P, R]:
        return Endpoint(
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
            retries=retries,
            retry_policy=retry_policy,
            retry_delay_seconds=retry_delay_seconds,
            workers=workers,
            concurrency=concurrency,
            keep_warm=keep_warm,
            max_pending_tasks=max_pending_tasks,
            callback_url=callback_url,
            authorized=authorized,
            env=env or {},
            secrets=secrets or [],
            volumes=volume_mounts(volumes or ()),
            on_start=on_start,
            autoscaler=autoscaler,
            task_policy=task_policy,
            checkpoint_enabled=checkpoint_enabled,
            inputs=inputs,
            outputs=outputs,
            docker_enabled=docker_enabled,
            preemptible=preemptible,
            region=region,
            availability_zone=availability_zone,
            machine=machine,
            metadata=metadata or {},
            route=route,
            domain=domain,
            methods=methods or list(DEFAULT_ENDPOINT_METHODS),
        )

    if func is None:
        return decorate
    return decorate(func)


@dataclass
class ASGI:
    app: Callable[..., Awaitable[Any]] | Callable[..., Any]
    _app_slug: str
    name: str = "asgi"
    image: Image = field(default_factory=Image)
    route: str = "/"
    domain: str | None = None
    cpu: CpuRequest | None = DEFAULT_HTTP_CPU
    memory: MemoryRequest | None = DEFAULT_HTTP_MEMORY
    disk: str | None = None
    gpu: GpuInput = None
    gpu_count: int = 0
    timeout_seconds: int | None = 180
    workers: int = 1
    concurrent_requests: int = 1
    keep_warm_seconds: int = 180
    max_pending_tasks: int = 100
    authorized: bool = True
    callback_url: str | None = None
    env: dict[str, str] = field(default_factory=dict)
    secrets: list[str] = field(default_factory=list)
    volumes: list[VolumeMount] = field(default_factory=list)
    on_start: LifecycleHookInput = None
    autoscaler: Autoscaler | Mapping[str, Any] | None = None
    task_policy: TaskPolicy | Mapping[str, Any] | None = None
    checkpoint_enabled: bool = False
    preemptible: bool = DEFAULT_WORKLOAD_PREEMPTIBLE
    region: str | None = None
    availability_zone: str = ""
    machine: MachineInput = None
    terminal: Terminal | None = field(default=None, init=False, repr=False)
    _handler_reference_target: Callable[..., Any] | None = field(
        default=None,
        init=False,
        repr=False,
    )
    _handler_reference_override: str | None = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        self._handler_reference_target = self.app
        update_wrapper(self, self.app)

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        if not is_local():
            return self.local(*args, **kwargs)
        raise EndpointOperationError(ENDPOINT_DIRECT_CALL_ERROR)

    def local(self, *args: Any, **kwargs: Any) -> Any:
        if _is_asgi_call(args, kwargs):
            return _call_asgi_compatible(self.app, args[0], args[1], args[2])
        return self.app(*args, **kwargs)

    def spec(self) -> DeploymentSpec:
        return DeploymentSpec(
            name=self.name,
            kind=DeploymentKind.Asgi,
            handler=self._handler_reference(),
            image=self.image.spec(),
            resources=Resources(
                region=ProductRegion(self.region) if self.region is not None else None,
                availability_zone=self.availability_zone,
                cpu=self.cpu,
                memory=self.memory,
                disk=self.disk or DEFAULT_DISK,
                gpu=list(gpu_preference(self.gpu)),
                gpu_count=self.gpu_count,
                timeout_seconds=_effective_timeout_seconds(self.task_policy, self.timeout_seconds),
                concurrency=self.concurrent_requests,
                keep_warm=self.keep_warm_seconds,
                preemptible=self.preemptible,
            ),
            route=self.route,
            domain=self.domain,
            env=self.env,
            secrets=self.secrets,
            volumes=list(self.volumes),
            lifecycle_hooks=lifecycle_hooks(on_start=self.on_start),
            metadata=build_resource_metadata(
                app=self._app_slug,
                workers=self.workers,
                max_pending_tasks=self.max_pending_tasks,
                authorized=self.authorized,
                callback_url=self.callback_url,
                autoscaler=self.autoscaler,
                task_policy=self.task_policy,
                checkpoint_enabled=self.checkpoint_enabled,
                machine=self.machine,
            ),
            client_contract=asgi_client_contract(),
        )

    @property
    def resource_name(self) -> str:
        return self.name

    _http_kind = "asgi"

    def effective_timeout_seconds(self) -> int | None:
        return _effective_timeout_seconds(self.task_policy, self.timeout_seconds)

    def unsupported_options(self) -> list[str]:
        """Declared options the platform cannot run yet, by name."""
        return unsupported_http_options(self)

    def require_supported(self) -> None:
        unsupported = self.unsupported_options()
        if unsupported:
            raise UnsupportedFeatureError(f"{self._http_kind} {self.name}", unsupported)

    def handler_reference(self) -> str:
        """The `module:qualname` the runner imports."""
        return self._handler_reference()

    def workload_spec(
        self, *, handler: str, source_sha256: str, image: ImageBuildResult
    ) -> WorkloadSpec:
        """The API definition of this app for an uploaded source and a ready image."""
        self.require_supported()
        return http_workload_spec(
            self,
            kind=self._http_kind,
            handler=handler,
            source_sha256=source_sha256,
            image=image,
            concurrency=self.concurrent_requests,
            keep_warm=self.keep_warm_seconds,
            max_pending_tasks=self.max_pending_tasks,
        )

    def deploy(
        self, *, workspace: str | None = None, source_root: str | Path | None = None
    ) -> Deployment:
        """Deploy this app into its LazyCloud app without touching other workloads."""
        return _deploy_http(self, workspace=workspace, source_root=source_root)

    def serve(self, timeout: int = 0, *, sync_dir: str | None = None) -> Preview:
        """Run a preview container that follows the working tree until Ctrl+C."""
        from lazycloud.abstractions.serve import serve_workload

        return serve_workload(
            self,
            kind=self._http_kind,
            authorized=self.authorized is not False,
            timeout=timeout,
            sync_dir=sync_dir or ".",
        )

    def request(
        self,
        *,
        method: str = "POST",
        path: str = "",
        json: object | None = None,
        data: bytes | str | None = None,
        headers: Mapping[str, str] | None = None,
        params: Mapping[str, object] | Iterable[tuple[str, object]] | None = None,
        target: InvocationTargetName = "auto",
        deployment_name: str | None = None,
        deployment_version: int | None = None,
    ) -> EndpointResponse:
        """Send one request to a route of the running preview or the deployment."""
        url, token, timeout = resolve_url(
            self,
            kind=self._http_kind,
            options=InvocationOptions(
                target=target,
                deployment_name=deployment_name,
                deployment_version=deployment_version,
            ),
        )
        return send_request(
            url,
            method=method,
            path=path,
            json_body=json,
            data=data,
            headers=headers,
            params=params,
            token=token,
            timeout_seconds=timeout,
        )

    def shell(
        self,
        *,
        workspace: str | None = None,
        container_id: str | None = None,
        sync_dir: str | None = None,
    ) -> ShellSession:
        """Open a shell container of this app's working-tree release, or `container_id`."""
        return _shell_http(self, workspace=workspace, container_id=container_id, sync_dir=sync_dir)

    def set_handler(self, handler: str) -> None:
        self._handler_reference_override = handler

    def _handler_reference(self) -> str:
        if self._handler_reference_override:
            return self._handler_reference_override
        target = self._handler_reference_target or self.app
        return dotted_reference(target)


@dataclass
class RealtimeASGI(ASGI):
    def __post_init__(self) -> None:
        source_handler = self.app
        self._handler_reference_target = source_handler
        self.app = _RealtimeWebSocketApp(source_handler)
        update_wrapper(self, source_handler)

    _http_kind = "realtime"

    def spec(self) -> DeploymentSpec:
        spec = super().spec()
        spec.metadata["realtime"] = True
        return spec


ASGIResourceT = TypeVar("ASGIResourceT", bound=ASGI)


def _asgi(
    *,
    resource_type: type[ASGIResourceT],
    _app_slug: str,
    name: str = "asgi",
    image: Image | None = None,
    route: str = "/",
    domain: str | None = None,
    cpu: CpuRequest | None = DEFAULT_HTTP_CPU,
    memory: MemoryRequest | None = DEFAULT_HTTP_MEMORY,
    disk: str | None = None,
    gpu: GpuInput = None,
    gpu_count: int = 0,
    timeout_seconds: int | None = 180,
    workers: int = 1,
    concurrent_requests: int = 1,
    keep_warm_seconds: int = 180,
    max_pending_tasks: int = 100,
    authorized: bool = True,
    callback_url: str | None = None,
    env: dict[str, str] | None = None,
    secrets: list[str] | None = None,
    volumes: Iterable[VolumeMount | VolumeExport] | None = None,
    on_start: LifecycleHookInput = None,
    autoscaler: Autoscaler | Mapping[str, Any] | None = None,
    task_policy: TaskPolicy | Mapping[str, Any] | None = None,
    checkpoint_enabled: bool = False,
    preemptible: bool = DEFAULT_WORKLOAD_PREEMPTIBLE,
    region: str | None = None,
    availability_zone: str = "",
    machine: MachineInput = None,
) -> Callable[[Callable[..., Any]], ASGIResourceT]:
    def decorate(target: Callable[..., Any]) -> ASGIResourceT:
        return resource_type(
            app=target,
            _app_slug=_app_slug,
            name=name,
            image=image or Image(),
            route=route,
            domain=domain,
            cpu=cpu,
            memory=memory,
            disk=disk,
            gpu=gpu,
            gpu_count=gpu_count,
            timeout_seconds=timeout_seconds,
            workers=workers,
            concurrent_requests=concurrent_requests,
            keep_warm_seconds=keep_warm_seconds,
            max_pending_tasks=max_pending_tasks,
            authorized=authorized,
            callback_url=callback_url,
            env=env or {},
            secrets=secrets or [],
            volumes=volume_mounts(volumes or ()),
            on_start=on_start,
            autoscaler=autoscaler,
            task_policy=task_policy,
            checkpoint_enabled=checkpoint_enabled,
            preemptible=preemptible,
            region=region,
            availability_zone=availability_zone,
            machine=machine,
        )

    return decorate


class _RealtimeWebSocketApp:
    def __init__(self, handler: Callable[..., Any]) -> None:
        self.handler = handler

    async def __call__(self, scope: ASGIMessage, receive: ASGIReceive, send: ASGISend) -> None:
        if scope.get("type") != "websocket":
            await _send_asgi_response(send, 404, b"not found")
            return
        context = _RealtimeContext(scope=scope)
        await send({"type": "websocket.accept"})
        while True:
            message = await receive()
            message_type = str(message.get("type", ""))
            if message_type == "websocket.disconnect":
                return
            if message_type == "websocket.connect":
                continue
            if message_type != "websocket.receive":
                continue
            output = _invoke_realtime_handler(self.handler, context, _websocket_payload(message))
            await _send_realtime_output(send, output)


@dataclass(frozen=True, slots=True)
class _RealtimeContext:
    scope: Mapping[str, Any]


async def _send_asgi_response(send: ASGISend, status_code: int, body: bytes) -> None:
    await send(
        {
            "type": "http.response.start",
            "status": status_code,
            "headers": [(b"content-type", b"text/plain; charset=utf-8")],
        }
    )
    await send({"type": "http.response.body", "body": body})


def _websocket_payload(message: ASGIMessage) -> str | bytes | Any:
    if "text" in message:
        return message["text"]
    if "bytes" in message:
        return message["bytes"]
    if "json" in message:
        return message["json"]
    return None


def _invoke_realtime_handler(
    handler: Callable[..., Any],
    context: _RealtimeContext,
    payload: Any,
) -> Any:
    if _callable_accepts_args(handler, 2):
        return handler(context, payload)
    if _callable_accepts_args(handler, 1):
        return handler(payload)
    return handler()


async def _send_realtime_output(send: ASGISend, output: Any) -> None:
    output = await _resolve_awaitable(output)
    if isinstance(output, RealtimeAsyncIterable):
        async for item in output:
            await _send_single_realtime_output(send, item)
        return
    if isinstance(output, RealtimeIterable) and _is_generator(output):
        for item in output:
            await _send_single_realtime_output(send, item)
        return
    await _send_single_realtime_output(send, output)


def _is_generator(value: RealtimeIterable) -> bool:
    return isinstance(value, types.GeneratorType)


async def _send_single_realtime_output(send: ASGISend, output: Any) -> None:
    output = await _resolve_awaitable(output)
    if output is None:
        return
    if isinstance(output, memoryview):
        await send({"type": "websocket.send", "bytes": output.tobytes()})
        return
    if isinstance(output, bytes | bytearray):
        await send({"type": "websocket.send", "bytes": bytes(output)})
        return
    if isinstance(output, str):
        await send({"type": "websocket.send", "text": output})
        return
    await send({"type": "websocket.send", "text": json.dumps(output)})


async def _resolve_awaitable(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


def _call_asgi_compatible(
    app: Callable[..., Any],
    scope: ASGIMessage,
    receive: ASGIReceive,
    send: ASGISend,
) -> Any:
    if _callable_accepts_args(app, 3):
        return app(scope, receive, send)
    candidate = _call_asgi_factory(app, scope)
    return candidate(scope, receive, send)


def _call_asgi_factory(app: Callable[..., Any], scope: ASGIMessage) -> Callable[..., Any]:
    candidate = app(_RealtimeContext(scope=scope)) if _callable_accepts_args(app, 1) else app()
    if not callable(candidate):
        msg = "ASGI handler must be an ASGI callable or return one"
        raise EndpointOperationError(msg)
    return candidate


def _is_asgi_call(args: tuple[Any, ...], kwargs: dict[str, Any]) -> bool:
    return len(args) == 3 and not kwargs and isinstance(args[0], Mapping)


def _callable_accepts_args(value: Callable[..., Any], count: int) -> bool:
    try:
        signature = inspect.signature(value)
    except (TypeError, ValueError):
        return True
    positional = 0
    for parameter in signature.parameters.values():
        if parameter.kind is inspect.Parameter.VAR_POSITIONAL:
            return True
        if parameter.kind in {
            inspect.Parameter.POSITIONAL_ONLY,
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
        }:
            positional += 1
    return positional >= count


def _effective_timeout_seconds(
    task_policy: TaskPolicy | Mapping[str, Any] | None,
    timeout_seconds: int | None,
) -> int | None:
    if isinstance(task_policy, TaskPolicy) and task_policy.timeout_seconds is not None:
        return task_policy.timeout_seconds
    if isinstance(task_policy, Mapping):
        value = task_policy.get("timeout_seconds")
        if isinstance(value, int):
            return value
    return timeout_seconds


__all__ = [
    "ASGI",
    "ASGIOptions",
    "Endpoint",
    "EndpointInvocation",
    "EndpointOperationError",
    "EndpointOptions",
    "EndpointResponse",
    "RealtimeASGI",
    "_asgi",
    "_endpoint",
]
