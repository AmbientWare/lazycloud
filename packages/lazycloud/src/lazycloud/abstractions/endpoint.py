from __future__ import annotations

import inspect
import json
import types
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from functools import update_wrapper
from pathlib import Path
from typing import (
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
from shared.autoscaling import QueueDepthAutoscaler
from shared.deployment_records import (
    DEFAULT_DISK,
    DEFAULT_HTTP_CPU,
    DEFAULT_HTTP_MEMORY,
    DeploymentSpec,
    Resources,
    VolumeMount,
    resolve_http_wait_timeout_seconds,
    resolve_timeout_seconds,
)
from shared.deployments import DeploymentKind
from shared.http.endpoints import StartEndpointServeResponse
from shared.http.errors import HttpTransportError
from shared.http.gateway import DeployStubResponse
from shared.serialization import to_json_value
from shared.tasks import RetryPolicy, TaskPolicy

from lazycloud.abstractions.function import FunctionOperationError
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
    sync_local_workspace,
    write_serve_preview,
)
from lazycloud.abstractions.shell import Shell, ShellSession
from lazycloud.abstractions.volume import VolumeExport, volume_mounts
from lazycloud.client_contracts import (
    asgi_client_contract,
    build_client_contract,
    schema_from_contract_parameters,
    schema_from_contract_return,
)
from lazycloud.clients.endpoint.control import EndpointControlClient
from lazycloud.clients.gateway.control import GatewayControlClient
from lazycloud.clients.resource.control import ResourceControlClient
from lazycloud.control import ControlClientConfig, resolve_control_client_config
from lazycloud.env import is_local
from lazycloud.http_transport import request_raw
from lazycloud.json_contracts import parse_json_value
from lazycloud.references import dotted_reference
from lazycloud.session.deployment import DeploymentClient, DeploymentControlClient
from lazycloud.terminal import Terminal

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
ENDPOINT_TRANSPORT_OVERHEAD_SECONDS = 5.0


class EndpointOperationError(FunctionOperationError):
    pass


class EndpointOptions(TypedDict, total=False):
    image: Image | None
    name: str | None
    route: str
    methods: list[str] | None
    cpu: float | None
    memory: str | None
    disk: str | None
    gpu: str | None
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


class ASGIOptions(TypedDict, total=False):
    name: str
    image: Image | None
    route: str
    cpu: float | None
    memory: str | None
    disk: str | None
    gpu: str | None
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
    autoscaler: QueueDepthAutoscaler | Mapping[str, Any] | None
    task_policy: TaskPolicy | Mapping[str, Any] | None
    checkpoint_enabled: bool
    pool: PoolInput
    provider: str | None


@dataclass(frozen=True, slots=True)
class EndpointResponse:
    status_code: int
    headers: Mapping[str, list[str]]
    content: bytes
    url: str

    @property
    def text(self) -> str:
        return self.content.decode("utf-8")

    def json(self) -> JsonValue:
        return parse_json_value(self.text)


@dataclass
class Endpoint(Generic[P, R]):
    func: Callable[P, R]
    _app_slug: str
    image: Image = field(default_factory=Image)
    name: str | None = None
    route: str = "/"
    methods: list[str] = field(default_factory=lambda: ["GET", "POST"])
    cpu: float | None = DEFAULT_HTTP_CPU
    memory: str | None = DEFAULT_HTTP_MEMORY
    disk: str | None = None
    gpu: str | None = None
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
    deployment_client: DeploymentControlClient | None = field(
        default=None,
        init=False,
        repr=False,
    )
    endpoint: str | None = field(default=None, init=False)
    token: str | None = field(default=None, init=False, repr=False)
    timeout: float = field(default=10.0, init=False)
    terminal: Terminal | None = field(default=None, init=False, repr=False)
    sync_local_dir: str | None = field(default=None, init=False)
    gateway_client: ServeGatewayClient | None = field(default=None, init=False, repr=False)
    resource_client: ServeResourceClient | None = field(default=None, init=False, repr=False)
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
        return self.func(*args, **kwargs)

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
                cpu=self.cpu,
                memory=self.memory,
                disk=self.disk or DEFAULT_DISK,
                gpu=self.gpu,
                gpu_count=self.gpu_count,
                timeout_seconds=_effective_timeout_seconds(self.task_policy, self.timeout_seconds),
                concurrency=self.concurrency,
                keep_warm=self.keep_warm,
                preemptible=self.preemptible,
            ),
            route=self.route,
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
        return spec

    def deploy(
        self,
        *,
        name: str | None = None,
        workspace: str | None = None,
        source_root: str | Path | None = None,
    ) -> DeployStubResponse:
        return _deploy_endpoint(
            self,
            name=name,
            workspace=workspace,
            label="endpoint",
            source_root=source_root,
        )

    def serve(self, timeout: int = 0, url_type: str = "") -> StartEndpointServeResponse:
        return _serve_endpoint(
            self,
            timeout=timeout,
            url_type=url_type,
            workspace=None,
            sync_dir=self.sync_local_dir if self.sync_local_dir is not None else ".",
            container_id=None,
            label="endpoint",
        )

    def request(
        self,
        *args: P.args,
        **kwargs: P.kwargs,
    ) -> EndpointResponse:
        return _request_function_endpoint(
            self,
            args=args,
            kwargs=kwargs,
            options=InvocationOptions(),
        )

    def target(
        self,
        target: InvocationTargetName = "auto",
        *,
        deployment_name: str | None = None,
        deployment_version: int | None = None,
    ) -> EndpointInvocation[P, R]:
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
        return _shell_endpoint(
            self,
            workspace=workspace,
            container_id=container_id,
            sync_dir=sync_dir,
            label="endpoint",
        )

    def set_handler(self, handler: str) -> None:
        self._handler_reference_override = handler

    def _handler_reference(self) -> str:
        return self._handler_reference_override or dotted_reference(self.func)

    def _config(self) -> ControlClientConfig:
        return resolve_control_client_config(
            endpoint=self.endpoint,
            token=self.token,
            timeout_seconds=self.timeout,
        )


@dataclass(frozen=True)
class EndpointInvocation(Generic[P, R]):
    owner: Endpoint[P, R]
    options: InvocationOptions

    def request(self, *args: P.args, **kwargs: P.kwargs) -> EndpointResponse:
        return _request_function_endpoint(
            self.owner,
            args=args,
            kwargs=kwargs,
            options=self.options,
        )


@overload
def _endpoint(
    func: Callable[P, R],
    *,
    _app_slug: str,
    image: Image | None = None,
    name: str | None = None,
    route: str = "/",
    methods: list[str] | None = None,
    cpu: float | None = DEFAULT_HTTP_CPU,
    memory: str | None = DEFAULT_HTTP_MEMORY,
    disk: str | None = None,
    gpu: str | None = None,
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
) -> Endpoint[P, R]: ...


@overload
def _endpoint(
    func: None = None,
    *,
    _app_slug: str,
    image: Image | None = None,
    name: str | None = None,
    route: str = "/",
    methods: list[str] | None = None,
    cpu: float | None = DEFAULT_HTTP_CPU,
    memory: str | None = DEFAULT_HTTP_MEMORY,
    disk: str | None = None,
    gpu: str | None = None,
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
) -> Callable[[Callable[P, R]], Endpoint[P, R]]: ...


def _endpoint(
    func: Callable[P, R] | None = None,
    *,
    _app_slug: str,
    image: Image | None = None,
    name: str | None = None,
    route: str = "/",
    methods: list[str] | None = None,
    cpu: float | None = DEFAULT_HTTP_CPU,
    memory: str | None = DEFAULT_HTTP_MEMORY,
    disk: str | None = None,
    gpu: str | None = None,
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
            pool=pool,
            provider=provider,
            metadata=metadata or {},
            route=route,
            methods=methods or ["GET", "POST"],
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
    cpu: float | None = DEFAULT_HTTP_CPU
    memory: str | None = DEFAULT_HTTP_MEMORY
    disk: str | None = None
    gpu: str | None = None
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
    autoscaler: QueueDepthAutoscaler | Mapping[str, Any] | None = None
    task_policy: TaskPolicy | Mapping[str, Any] | None = None
    checkpoint_enabled: bool = False
    pool: PoolInput = None
    provider: str | None = None
    deployment_client: DeploymentControlClient | None = field(
        default=None,
        init=False,
        repr=False,
    )
    endpoint: str | None = field(default=None, init=False)
    token: str | None = field(default=None, init=False, repr=False)
    timeout: float = field(default=10.0, init=False)
    stub_id: str = field(default="", init=False)
    terminal: Terminal | None = field(default=None, init=False, repr=False)
    sync_local_dir: str | None = field(default=None, init=False)
    gateway_client: ServeGatewayClient | None = field(default=None, init=False, repr=False)
    resource_client: ServeResourceClient | None = field(default=None, init=False, repr=False)
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
                cpu=self.cpu,
                memory=self.memory,
                disk=self.disk or DEFAULT_DISK,
                gpu=self.gpu,
                gpu_count=self.gpu_count,
                timeout_seconds=_effective_timeout_seconds(self.task_policy, self.timeout_seconds),
                concurrency=self.concurrent_requests,
                keep_warm=self.keep_warm_seconds,
            ),
            route=self.route,
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
                pool=self.pool,
                provider=self.provider,
            ),
            client_contract=asgi_client_contract(),
        )

    def deploy(
        self,
        *,
        name: str | None = None,
        workspace: str | None = None,
        source_root: str | Path | None = None,
    ) -> DeployStubResponse:
        return _deploy_endpoint(
            self,
            name=name,
            workspace=workspace,
            label="ASGI app",
            source_root=source_root,
        )

    def serve(self, timeout: int = 0, url_type: str = "") -> StartEndpointServeResponse:
        return _serve_endpoint(
            self,
            timeout=timeout,
            url_type=url_type,
            workspace=None,
            sync_dir=self.sync_local_dir if self.sync_local_dir is not None else ".",
            container_id=None,
            label="ASGI app",
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
        return _request_http_endpoint(
            self,
            method=method,
            path=path,
            json_body=json,
            data=data,
            headers=headers,
            params=params,
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
        return _shell_endpoint(
            self,
            workspace=workspace,
            container_id=container_id,
            sync_dir=sync_dir,
            label="ASGI app",
        )

    def set_handler(self, handler: str) -> None:
        self._handler_reference_override = handler

    def _handler_reference(self) -> str:
        if self._handler_reference_override:
            return self._handler_reference_override
        target = self._handler_reference_target or self.app
        return dotted_reference(target)

    def _config(self) -> ControlClientConfig:
        return resolve_control_client_config(
            endpoint=self.endpoint,
            token=self.token,
            timeout_seconds=self.timeout,
        )


@dataclass
class RealtimeASGI(ASGI):
    def __post_init__(self) -> None:
        source_handler = self.app
        self._handler_reference_target = source_handler
        self.app = _RealtimeWebSocketApp(source_handler)
        update_wrapper(self, source_handler)

    def spec(self) -> DeploymentSpec:
        spec = super().spec()
        spec.metadata["realtime"] = True
        return spec


def _asgi(
    *,
    _app_slug: str,
    name: str = "asgi",
    image: Image | None = None,
    route: str = "/",
    cpu: float | None = DEFAULT_HTTP_CPU,
    memory: str | None = DEFAULT_HTTP_MEMORY,
    disk: str | None = None,
    gpu: str | None = None,
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
    autoscaler: QueueDepthAutoscaler | Mapping[str, Any] | None = None,
    task_policy: TaskPolicy | Mapping[str, Any] | None = None,
    checkpoint_enabled: bool = False,
    pool: PoolInput = None,
    provider: str | None = None,
) -> Callable[[Callable[..., Awaitable[Any]] | Callable[..., Any]], ASGI]:
    def decorate(target: Callable[..., Awaitable[Any]] | Callable[..., Any]) -> ASGI:
        return ASGI(
            app=target,
            _app_slug=_app_slug,
            name=name,
            image=image or Image(),
            route=route,
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
            pool=pool,
            provider=provider,
        )

    return decorate


def _realtime(
    *,
    _app_slug: str,
    name: str = "realtime",
    image: Image | None = None,
    route: str = "/",
    cpu: float | None = DEFAULT_HTTP_CPU,
    memory: str | None = DEFAULT_HTTP_MEMORY,
    disk: str | None = None,
    gpu: str | None = None,
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
    autoscaler: QueueDepthAutoscaler | Mapping[str, Any] | None = None,
    task_policy: TaskPolicy | Mapping[str, Any] | None = None,
    checkpoint_enabled: bool = False,
    pool: PoolInput = None,
    provider: str | None = None,
) -> Callable[[Callable[..., Any]], RealtimeASGI]:
    def decorate(target: Callable[..., Any]) -> RealtimeASGI:
        return RealtimeASGI(
            app=target,
            _app_slug=_app_slug,
            name=name,
            image=image or Image(),
            route=route,
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
            pool=pool,
            provider=provider,
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


def _request_function_endpoint(
    owner: Endpoint[..., Any] | ASGI,
    *,
    args: tuple[Any, ...],
    kwargs: Mapping[str, Any],
    options: InvocationOptions,
) -> EndpointResponse:
    payload = _endpoint_request_payload(args=args, kwargs=kwargs)
    return _request_http_endpoint(
        owner,
        method=_endpoint_request_method(owner),
        path="",
        json_body=payload,
        data=None,
        headers=None,
        params=None,
        options=options,
    )


def _request_http_endpoint(
    owner: Endpoint[..., Any] | ASGI,
    *,
    method: str,
    path: str,
    json_body: object | None,
    data: bytes | str | None,
    headers: Mapping[str, str] | None,
    params: Mapping[str, object] | Iterable[tuple[str, object]] | None,
    options: InvocationOptions,
) -> EndpointResponse:
    config = owner._config()
    spec = owner.spec()
    resolved = _resolve_endpoint_invocation_target(
        owner,
        spec=spec,
        config=config,
        options=options,
    )
    try:
        response = request_raw(
            resolved.url,
            method=method,
            path=path,
            json_body=json_body,
            data=data,
            headers=headers,
            params=params,
            token=config.token,
            timeout_seconds=_endpoint_transport_timeout_seconds(spec),
        )
    except (HttpTransportError, ValueError) as exc:
        raise EndpointOperationError(str(exc)) from exc
    return EndpointResponse(
        status_code=response.status_code,
        headers=response.headers,
        content=response.content,
        url=response.final_url,
    )


def _resolve_endpoint_invocation_target(
    owner: Endpoint[..., Any] | ASGI,
    *,
    spec: DeploymentSpec,
    config: ControlClientConfig,
    options: InvocationOptions,
) -> InvocationTarget:
    preview_client = owner.resource_client or ResourceControlClient.from_endpoint(
        config.endpoint,
        token=config.token,
        timeout_seconds=config.timeout_seconds,
        workspace=config.workspace,
    )
    try:
        return resolve_invocation_target(
            kind=spec.kind,
            name=spec.name,
            app=owner._app_slug,
            config=config,
            deployment_client=owner.deployment_client,
            preview_client=preview_client,
            target=options.target,
            deployment_name=options.deployment_name,
            deployment_version=options.deployment_version,
        )
    except InvocationTargetError as exc:
        raise EndpointOperationError(str(exc)) from exc


def _endpoint_request_payload(
    *,
    args: tuple[Any, ...],
    kwargs: Mapping[str, Any],
) -> dict[str, JsonValue]:
    return {
        "args": [to_json_value(arg) for arg in args],
        "kwargs": {key: to_json_value(value) for key, value in kwargs.items()},
    }


def _endpoint_request_method(owner: Endpoint[..., Any] | ASGI) -> str:
    if not isinstance(owner, Endpoint):
        return "POST"
    methods = [method.strip().upper() for method in owner.methods if method.strip()]
    if "POST" in methods:
        return "POST"
    return methods[0] if methods else "POST"


def _deploy_endpoint(
    owner: Endpoint[..., Any] | ASGI,
    *,
    name: str | None = None,
    workspace: str | None,
    label: str,
    source_root: str | Path | None = None,
) -> DeployStubResponse:
    try:
        response = DeploymentClient(
            client=owner.deployment_client,
            workspace=workspace,
            endpoint=owner.endpoint,
            token=owner.token,
            timeout_seconds=owner.timeout,
            sync_source=True,
            terminal=owner.terminal,
        ).create(
            owner.spec(),
            name=name,
            workspace=workspace,
            image=owner.image,
            source_root=source_root,
        )
    except RuntimeError as exc:
        raise EndpointOperationError(str(exc)) from exc
    owner.stub_id = response.stub_id or owner.stub_id
    return response


def _prepare_endpoint(
    owner: Endpoint[..., Any] | ASGI,
    *,
    workspace: str | None,
    label: str,
    source_root: str | None = None,
) -> str:
    try:
        response = DeploymentClient(
            client=owner.deployment_client,
            workspace=workspace,
            endpoint=owner.endpoint,
            token=owner.token,
            timeout_seconds=owner.timeout,
            sync_source=True,
            terminal=owner.terminal,
        ).prepare(
            owner.spec(),
            workspace=workspace,
            image=owner.image,
            source_root=source_root,
        )
    except RuntimeError as exc:
        raise EndpointOperationError(str(exc)) from exc
    if not response.stub_id:
        msg = f"deployment prepare did not return a {label} stub_id"
        raise EndpointOperationError(msg)
    owner.stub_id = response.stub_id
    return owner.stub_id


def _serve_endpoint(
    owner: Endpoint[..., Any] | ASGI,
    *,
    timeout: int,
    url_type: str,
    workspace: str | None,
    sync_dir: str | None,
    container_id: str | None,
    label: str,
) -> StartEndpointServeResponse:
    terminal = owner.terminal or Terminal()
    owner.terminal = terminal
    source_root = sync_dir or None
    stub_id = owner.stub_id or _prepare_endpoint(
        owner,
        workspace=workspace,
        label=label,
        source_root=source_root,
    )
    config = owner._config()
    gateway_client = owner.gateway_client or GatewayControlClient.from_endpoint(
        config.endpoint,
        token=config.token,
        timeout_seconds=config.timeout_seconds,
    )
    resource_client = owner.resource_client or ResourceControlClient.from_endpoint(
        config.endpoint,
        token=config.token,
        timeout_seconds=config.timeout_seconds,
        workspace=config.workspace,
    )
    serve_url = resolve_serve_url(
        gateway_client,
        stub_id=stub_id,
        url_type=url_type,
        workspace=workspace,
        external_url=config.endpoint,
    )
    response = EndpointControlClient.from_endpoint(
        config.endpoint,
        token=config.token,
        timeout_seconds=config.timeout_seconds,
    ).start_serve(stub_id, timeout=timeout)
    selected_container_id = container_id or response.container_id
    if not selected_container_id:
        raise EndpointOperationError(f"serve did not return a {label} container_id")
    spec = owner.spec()
    preview_record = write_serve_preview(
        kind=spec.kind,
        name=spec.name,
        app=owner._app_slug,
        workspace=config.workspace,
        endpoint=config.endpoint,
        stub_id=stub_id,
        container_id=selected_container_id,
        url=serve_url.url,
    )
    ServePreviewSession(
        stub_id=stub_id,
        container_id=selected_container_id,
        url=serve_url.url,
        gateway_client=gateway_client,
        resource_client=resource_client,
        terminal=terminal,
        sync_dir=sync_dir,
        token=owner.token,
        authorized=owner.authorized is not False,
        preview_record=preview_record,
    ).run()
    return response


def _shell_endpoint(
    owner: Endpoint[..., Any] | ASGI,
    *,
    workspace: str | None,
    container_id: str | None,
    sync_dir: str | None,
    label: str,
) -> ShellSession:
    shell = Shell(
        workspace=workspace,
        endpoint=owner.endpoint,
        token=owner.token,
        timeout_seconds=owner.timeout,
    )
    if container_id:
        session = shell.create_existing(container_id)
    else:
        session = shell.create_standalone(
            owner.stub_id or _prepare_endpoint(owner, workspace=workspace, label=label)
        )
    if sync_dir:
        _sync_shell_dir(
            session.container_id,
            sync_dir,
            owner=owner,
        )
    return session


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


def _endpoint_transport_timeout_seconds(spec: DeploymentSpec) -> float:
    return (
        resolve_http_wait_timeout_seconds(
            resolve_timeout_seconds(spec.kind, spec.resources.timeout_seconds)
        )
        + ENDPOINT_TRANSPORT_OVERHEAD_SECONDS
    )


def _sync_shell_dir(
    container_id: str,
    sync_dir: str | None,
    *,
    owner: Endpoint[..., Any] | ASGI,
) -> None:
    if not sync_dir or not container_id:
        return
    config = owner._config()
    gateway_client = owner.gateway_client or GatewayControlClient.from_endpoint(
        config.endpoint,
        token=config.token,
        timeout_seconds=config.timeout_seconds,
    )
    sync_local_workspace(
        container_id=container_id,
        local_dir=sync_dir,
        gateway_client=gateway_client,
        terminal=owner.terminal,
    )


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
    "_realtime",
]
