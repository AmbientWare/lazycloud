from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass, fields, is_dataclass
from pathlib import Path
from typing import Any, ParamSpec, Protocol, TypeVar, overload, runtime_checkable

from pydantic import JsonValue
from shared.app_slug import validate_app_slug
from shared.autoscaling import QueueDepthAutoscaler
from shared.deployment_records import (
    DEFAULT_FUNCTION_AUTHORIZED,
    DEFAULT_FUNCTION_CPU,
    DEFAULT_FUNCTION_MEMORY,
    DEFAULT_FUNCTION_RETRIES,
    DEFAULT_FUNCTION_TIMEOUT_SECONDS,
    DEFAULT_HTTP_CPU,
    DEFAULT_HTTP_MEMORY,
    DEFAULT_TASK_QUEUE_CPU,
    DEFAULT_TASK_QUEUE_MEMORY,
    DeploymentSpec,
    VolumeMount,
)
from shared.deployments import DeploymentKind
from shared.serialization import to_json_value
from shared.tasks import RetryPolicy, TaskPolicy

from lazycloud.abstractions.endpoint import (
    ASGI,
    ASGIOptions,
    Endpoint,
    EndpointOptions,
    RealtimeASGI,
)
from lazycloud.abstractions.endpoint import _asgi as asgi_decorator
from lazycloud.abstractions.endpoint import _endpoint as endpoint_decorator
from lazycloud.abstractions.endpoint import _realtime as realtime_decorator
from lazycloud.abstractions.function import CronJob, Function, FunctionOptions
from lazycloud.abstractions.function import _cron as cron_decorator
from lazycloud.abstractions.function import _function as function_decorator
from lazycloud.abstractions.image import Image
from lazycloud.abstractions.metadata import (
    LifecycleHookInput,
    PlacementInput,
    PoolInput,
    RetryPolicyInput,
    SchemaInput,
)
from lazycloud.abstractions.pod import Pod, PodOptions
from lazycloud.abstractions.sandbox import Sandbox, SandboxOptions
from lazycloud.abstractions.taskqueue import TaskQueueFunction, TaskQueueOptions
from lazycloud.abstractions.taskqueue import _task_queue as task_queue_decorator
from lazycloud.abstractions.volume import VolumeExport, volume_mounts


class AppOperationError(RuntimeError):
    pass


class AppResource(Protocol):
    placement: PlacementInput

    def spec(self) -> DeploymentSpec: ...


ResourceT = TypeVar("ResourceT", bound=AppResource)
P = ParamSpec("P")
R = TypeVar("R")


@runtime_checkable
class ModelDumpable(Protocol):
    def model_dump(self, *, mode: str = "python") -> object: ...


@dataclass(frozen=True, slots=True)
class AppDeployResult:
    app: str
    resources: tuple[object, ...]

    def model_dump(self, *, mode: str = "python") -> dict[str, JsonValue]:
        _ = mode
        return {
            "app": self.app,
            "resources": [to_json_value(_dump_resource(item)) for item in self.resources],
        }


class App:
    """Owns the deployable resources registered under one application slug."""

    def __init__(self, slug: str) -> None:
        """Create an app namespace for functions, endpoints, queues, pods, and sandboxes.

        The slug is the stable production identity used by deploy, serve, and
        generated client handles. Use a short lowercase slug such as
        `"billing"` or `"reporting-api"`.
        """
        self.slug = validate_app_slug(slug)
        self._resources: dict[tuple[DeploymentKind, str], AppResource] = {}

    @property
    def resources(self) -> tuple[AppResource, ...]:
        return tuple(self._resources.values())

    @overload
    def function(
        self,
        func: Callable[P, R],
        *,
        image: Image | None = None,
        name: str | None = None,
        cpu: float | None = DEFAULT_FUNCTION_CPU,
        memory: str | None = DEFAULT_FUNCTION_MEMORY,
        gpu: str | None = None,
        gpu_count: int = 0,
        timeout_seconds: int | None = DEFAULT_FUNCTION_TIMEOUT_SECONDS,
        retries: int = DEFAULT_FUNCTION_RETRIES,
        retry_policy: RetryPolicyInput = None,
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
        on_cancelled: LifecycleHookInput = None,
        on_timeout: LifecycleHookInput = None,
        on_finish: LifecycleHookInput = None,
        task_policy: TaskPolicy | Mapping[str, Any] | None = None,
        inputs: SchemaInput = None,
        outputs: SchemaInput = None,
        docker_enabled: bool = False,
        preemptible: bool = False,
        pool: PoolInput = None,
        placement: PlacementInput = None,
        provider: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> Function[P, R]: ...

    @overload
    def function(
        self,
        func: None = None,
        *,
        image: Image | None = None,
        name: str | None = None,
        cpu: float | None = DEFAULT_FUNCTION_CPU,
        memory: str | None = DEFAULT_FUNCTION_MEMORY,
        gpu: str | None = None,
        gpu_count: int = 0,
        timeout_seconds: int | None = DEFAULT_FUNCTION_TIMEOUT_SECONDS,
        retries: int = DEFAULT_FUNCTION_RETRIES,
        retry_policy: RetryPolicyInput = None,
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
        on_cancelled: LifecycleHookInput = None,
        on_timeout: LifecycleHookInput = None,
        on_finish: LifecycleHookInput = None,
        task_policy: TaskPolicy | Mapping[str, Any] | None = None,
        inputs: SchemaInput = None,
        outputs: SchemaInput = None,
        docker_enabled: bool = False,
        preemptible: bool = False,
        pool: PoolInput = None,
        placement: PlacementInput = None,
        provider: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> Callable[[Callable[P, R]], Function[P, R]]: ...

    def function(
        self,
        func: Callable[P, R] | None = None,
        *,
        image: Image | None = None,
        name: str | None = None,
        cpu: float | None = DEFAULT_FUNCTION_CPU,
        memory: str | None = DEFAULT_FUNCTION_MEMORY,
        gpu: str | None = None,
        gpu_count: int = 0,
        timeout_seconds: int | None = DEFAULT_FUNCTION_TIMEOUT_SECONDS,
        retries: int = DEFAULT_FUNCTION_RETRIES,
        retry_policy: RetryPolicyInput = None,
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
        on_cancelled: LifecycleHookInput = None,
        on_timeout: LifecycleHookInput = None,
        on_finish: LifecycleHookInput = None,
        task_policy: TaskPolicy | Mapping[str, Any] | None = None,
        inputs: SchemaInput = None,
        outputs: SchemaInput = None,
        docker_enabled: bool = False,
        preemptible: bool = False,
        pool: PoolInput = None,
        placement: PlacementInput = None,
        provider: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> Function[P, R] | Callable[[Callable[P, R]], Function[P, R]]:
        """Register a Python callable as an app-owned remote function.

        Use `@app.function` for Python work that should run remotely but does
        not need an HTTP route or queue. The returned object preserves the
        callable's argument and return typing, supports local execution with
        `.local(...)`, and deploys under this app's slug.

        Args:
            func: Callable to register when using `app.function(fn)` directly.
            image: Image definition used to build or select the runtime image.
            name: Deployment resource name. Defaults to the callable name.
            cpu, memory, gpu, gpu_count: Compute resources requested per worker.
            timeout_seconds: Maximum runtime for one invocation.
            retries: Number of retry attempts for failed invocations.
            callback_url: Optional webhook called for execution events.
            authorized: Whether calls require an authenticated client.
            env, secrets, volumes: Runtime configuration injected into workers.
            on_start and task lifecycle hooks: Hooks invoked by the workload runner.
            task_policy: Scheduling policy for invocation retries and timeouts.
            inputs, outputs: Optional schema metadata for clients and validation.
            docker_enabled: Whether the execution container needs an isolated Docker daemon.
            pool, placement, provider, metadata: Compute placement and custom metadata.
        """
        kwargs = _function_options(
            image=image,
            name=name,
            cpu=cpu,
            memory=memory,
            gpu=gpu,
            gpu_count=gpu_count,
            timeout_seconds=timeout_seconds,
            retries=retries,
            retry_policy=retry_policy,
            retry_delay_seconds=retry_delay_seconds,
            callback_url=callback_url,
            authorized=authorized,
            env=env,
            secrets=secrets,
            volumes=volumes,
            on_start=on_start,
            on_running=on_running,
            on_success=on_success,
            on_error=on_error,
            on_retry=on_retry,
            on_failure=on_failure,
            on_cancelled=on_cancelled,
            on_timeout=on_timeout,
            on_finish=on_finish,
            task_policy=task_policy,
            inputs=inputs,
            outputs=outputs,
            docker_enabled=docker_enabled,
            preemptible=preemptible,
            pool=pool,
            placement=placement,
            provider=provider,
            metadata=metadata,
        )

        def decorate(target: Callable[P, R]) -> Function[P, R]:
            return self._register(function_decorator(target, _app_slug=self.slug, **kwargs))

        return decorate if func is None else decorate(func)

    def cron(
        self,
        cron: str,
        *,
        image: Image | None = None,
        name: str | None = None,
        cpu: float | None = DEFAULT_FUNCTION_CPU,
        memory: str | None = DEFAULT_FUNCTION_MEMORY,
        gpu: str | None = None,
        gpu_count: int = 0,
        timeout_seconds: int | None = DEFAULT_FUNCTION_TIMEOUT_SECONDS,
        retries: int = DEFAULT_FUNCTION_RETRIES,
        retry_policy: RetryPolicyInput = None,
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
        on_cancelled: LifecycleHookInput = None,
        on_timeout: LifecycleHookInput = None,
        on_finish: LifecycleHookInput = None,
        task_policy: TaskPolicy | Mapping[str, Any] | None = None,
        inputs: SchemaInput = None,
        outputs: SchemaInput = None,
        docker_enabled: bool = False,
        pool: PoolInput = None,
        placement: PlacementInput = None,
        provider: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> Callable[[Callable[P, R]], CronJob[P, R]]:
        """Register a scheduled function owned by this app.

        The schedule uses standard cron syntax. The decorated callable keeps its
        normal Python signature for local execution, while deploy registers it
        as a scheduled app resource.

        Args:
            cron: Cron expression that controls when the function runs.
            image: Image definition used to build or select the runtime image.
            name: Deployment resource name. Defaults to the callable name.
            cpu, memory, gpu, gpu_count: Compute resources requested per run.
            timeout_seconds: Maximum runtime for one scheduled invocation.
            retries: Number of retry attempts for failed scheduled runs.
            callback_url: Optional webhook called for execution events.
            authorized: Whether calls require an authenticated client.
            env, secrets, volumes: Runtime configuration injected into workers.
            on_start and task lifecycle hooks: Hooks invoked by the workload runner.
            task_policy: Scheduling policy for invocation retries and timeouts.
            inputs, outputs: Optional schema metadata for clients and validation.
            docker_enabled: Whether the execution container needs an isolated Docker daemon.
            pool, placement, provider, metadata: Compute placement and custom metadata.
        """
        kwargs = _function_options(
            image=image,
            name=name,
            cpu=cpu,
            memory=memory,
            gpu=gpu,
            gpu_count=gpu_count,
            timeout_seconds=timeout_seconds,
            retries=retries,
            retry_policy=retry_policy,
            retry_delay_seconds=retry_delay_seconds,
            callback_url=callback_url,
            authorized=authorized,
            env=env,
            secrets=secrets,
            volumes=volumes,
            on_start=on_start,
            on_running=on_running,
            on_success=on_success,
            on_error=on_error,
            on_retry=on_retry,
            on_failure=on_failure,
            on_cancelled=on_cancelled,
            on_timeout=on_timeout,
            on_finish=on_finish,
            task_policy=task_policy,
            inputs=inputs,
            outputs=outputs,
            docker_enabled=docker_enabled,
            preemptible=False,
            pool=pool,
            placement=placement,
            provider=provider,
            metadata=metadata,
        )

        def decorate(target: Callable[P, R]) -> CronJob[P, R]:
            return self._register(cron_decorator(cron, _app_slug=self.slug, **kwargs)(target))

        return decorate

    @overload
    def endpoint(
        self,
        func: Callable[P, R],
        *,
        image: Image | None = None,
        name: str | None = None,
        route: str = "/",
        methods: list[str] | None = None,
        cpu: float | None = DEFAULT_HTTP_CPU,
        memory: str | None = DEFAULT_HTTP_MEMORY,
        gpu: str | None = None,
        gpu_count: int = 0,
        timeout_seconds: int | None = 180,
        retries: int = 0,
        retry_policy: RetryPolicyInput = None,
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
        placement: PlacementInput = None,
        provider: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> Endpoint[P, R]: ...

    @overload
    def endpoint(
        self,
        func: None = None,
        *,
        image: Image | None = None,
        name: str | None = None,
        route: str = "/",
        methods: list[str] | None = None,
        cpu: float | None = DEFAULT_HTTP_CPU,
        memory: str | None = DEFAULT_HTTP_MEMORY,
        gpu: str | None = None,
        gpu_count: int = 0,
        timeout_seconds: int | None = 180,
        retries: int = 0,
        retry_policy: RetryPolicyInput = None,
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
        placement: PlacementInput = None,
        provider: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> Callable[[Callable[P, R]], Endpoint[P, R]]: ...

    def endpoint(
        self,
        func: Callable[P, R] | None = None,
        *,
        image: Image | None = None,
        name: str | None = None,
        route: str = "/",
        methods: list[str] | None = None,
        cpu: float | None = DEFAULT_HTTP_CPU,
        memory: str | None = DEFAULT_HTTP_MEMORY,
        gpu: str | None = None,
        gpu_count: int = 0,
        timeout_seconds: int | None = 180,
        retries: int = 0,
        retry_policy: RetryPolicyInput = None,
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
        placement: PlacementInput = None,
        provider: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> Endpoint[P, R] | Callable[[Callable[P, R]], Endpoint[P, R]]:
        """Register a typed Python callable as an HTTP endpoint.

        Use `@app.endpoint` when the callable should be invoked over HTTP.
        The returned endpoint supports `.request(...)` for served or deployed
        calls and `.local(...)` for in-process execution.

        Args:
            func: Callable to register when using `app.endpoint(fn)` directly.
            image: Image definition used to build or select the runtime image.
            name: Deployment resource name. Defaults to the callable name.
            route: HTTP route mounted for this endpoint.
            methods: HTTP methods accepted by the route. Defaults to framework policy.
            cpu, memory, gpu, gpu_count: Compute resources requested per worker.
            timeout_seconds: Maximum request runtime.
            retries: Retry attempts for failed request handling.
            workers, concurrency: Worker count and concurrent requests per worker.
            keep_warm: Seconds to keep idle workers available.
            max_pending_tasks: Queue backpressure limit for pending requests.
            callback_url: Optional webhook called for execution events.
            authorized: Whether requests require authentication.
            env, secrets, volumes: Runtime configuration injected into workers.
            on_start: Startup hook invoked by the workload runner.
            autoscaler, task_policy: Scheduling policies.
            inputs, outputs: Optional schema metadata for clients and validation.
            docker_enabled: Whether the execution container needs an isolated Docker daemon.
            pool, placement, provider, metadata: Compute placement and custom metadata.
        """
        kwargs = _endpoint_options(
            image=image,
            name=name,
            route=route,
            methods=methods,
            cpu=cpu,
            memory=memory,
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
            env=env,
            secrets=secrets,
            volumes=volumes,
            on_start=on_start,
            autoscaler=autoscaler,
            task_policy=task_policy,
            checkpoint_enabled=checkpoint_enabled,
            inputs=inputs,
            outputs=outputs,
            docker_enabled=docker_enabled,
            preemptible=preemptible,
            pool=pool,
            placement=placement,
            provider=provider,
            metadata=metadata,
        )

        def decorate(target: Callable[P, R]) -> Endpoint[P, R]:
            return self._register(endpoint_decorator(target, _app_slug=self.slug, **kwargs))

        return decorate if func is None else decorate(func)

    def asgi(
        self,
        *,
        name: str = "asgi",
        image: Image | None = None,
        route: str = "/",
        cpu: float | None = DEFAULT_HTTP_CPU,
        memory: str | None = DEFAULT_HTTP_MEMORY,
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
        placement: PlacementInput = None,
        provider: str | None = None,
    ) -> Callable[[Callable[..., Awaitable[Any]] | Callable[..., Any]], ASGI]:
        """Register an ASGI application owned by this app.

        Use this for FastAPI, Starlette, or raw ASGI apps. The decorated app is
        deployed as an HTTP-capable resource and exposes `.request(...)` for
        served or deployed calls.

        Args:
            name: Deployment resource name for the ASGI app.
            image: Image definition used to build or select the runtime image.
            route: Route prefix mounted for the ASGI app.
            cpu, memory, gpu, gpu_count: Compute resources requested per worker.
            timeout_seconds: Maximum request runtime.
            workers, concurrent_requests: Worker count and requests per worker.
            keep_warm_seconds: Seconds to keep idle workers available.
            max_pending_tasks: Queue backpressure limit for pending requests.
            authorized: Whether requests require authentication.
            callback_url: Optional webhook called for execution events.
            env, secrets, volumes: Runtime configuration injected into workers.
            on_start: Startup hook invoked by the workload runner.
            autoscaler, task_policy: Scheduling policies.
            pool, placement, provider: Compute placement options.
        """
        kwargs = _asgi_options(
            name=name,
            image=image,
            route=route,
            cpu=cpu,
            memory=memory,
            gpu=gpu,
            gpu_count=gpu_count,
            timeout_seconds=timeout_seconds,
            workers=workers,
            concurrent_requests=concurrent_requests,
            keep_warm_seconds=keep_warm_seconds,
            max_pending_tasks=max_pending_tasks,
            authorized=authorized,
            callback_url=callback_url,
            env=env,
            secrets=secrets,
            volumes=volumes,
            on_start=on_start,
            autoscaler=autoscaler,
            task_policy=task_policy,
            checkpoint_enabled=checkpoint_enabled,
            pool=pool,
            placement=placement,
            provider=provider,
        )
        factory = asgi_decorator(_app_slug=self.slug, **kwargs)

        def decorate(
            target: Callable[..., Awaitable[Any]] | Callable[..., Any],
        ) -> ASGI:
            return self._register(factory(target))

        return decorate

    def realtime(
        self,
        *,
        name: str = "realtime",
        image: Image | None = None,
        route: str = "/",
        cpu: float | None = DEFAULT_HTTP_CPU,
        memory: str | None = DEFAULT_HTTP_MEMORY,
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
        placement: PlacementInput = None,
        provider: str | None = None,
    ) -> Callable[[Callable[..., Any]], RealtimeASGI]:
        """Register a realtime WebSocket-style handler owned by this app.

        The decorated handler is wrapped as an ASGI resource and can be served
        or deployed alongside the rest of the app.

        Args:
            name: Deployment resource name for the realtime endpoint.
            image: Image definition used to build or select the runtime image.
            route: Route prefix mounted for realtime traffic.
            cpu, memory, gpu, gpu_count: Compute resources requested per worker.
            timeout_seconds: Maximum connection or message handling runtime.
            workers, concurrent_requests: Worker count and concurrent connections.
            keep_warm_seconds: Seconds to keep idle workers available.
            max_pending_tasks: Queue backpressure limit for pending work.
            authorized: Whether requests require authentication.
            callback_url: Optional webhook called for execution events.
            env, secrets, volumes: Runtime configuration injected into workers.
            on_start: Startup hook invoked by the workload runner.
            autoscaler, task_policy: Scheduling policies.
            pool, placement, provider: Compute placement options.
        """
        kwargs = _asgi_options(
            name=name,
            image=image,
            route=route,
            cpu=cpu,
            memory=memory,
            gpu=gpu,
            gpu_count=gpu_count,
            timeout_seconds=timeout_seconds,
            workers=workers,
            concurrent_requests=concurrent_requests,
            keep_warm_seconds=keep_warm_seconds,
            max_pending_tasks=max_pending_tasks,
            authorized=authorized,
            callback_url=callback_url,
            env=env,
            secrets=secrets,
            volumes=volumes,
            on_start=on_start,
            autoscaler=autoscaler,
            task_policy=task_policy,
            checkpoint_enabled=checkpoint_enabled,
            pool=pool,
            placement=placement,
            provider=provider,
        )
        factory = realtime_decorator(_app_slug=self.slug, **kwargs)

        def decorate(target: Callable[..., Any]) -> RealtimeASGI:
            return self._register(factory(target))

        return decorate

    @overload
    def task_queue(
        self,
        func: Callable[P, R],
        *,
        image: Image | None = None,
        name: str | None = None,
        cpu: float | None = DEFAULT_TASK_QUEUE_CPU,
        memory: str | None = DEFAULT_TASK_QUEUE_MEMORY,
        gpu: str | None = None,
        gpu_count: int = 0,
        timeout: int | None = 3600,
        retries: int = 3,
        retry_for: Iterable[type[BaseException]] | None = None,
        retry_delay_seconds: float = 0.0,
        retry_policy: RetryPolicyInput = None,
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
        placement: PlacementInput = None,
        provider: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> TaskQueueFunction[P, R]: ...

    @overload
    def task_queue(
        self,
        func: None = None,
        *,
        image: Image | None = None,
        name: str | None = None,
        cpu: float | None = DEFAULT_TASK_QUEUE_CPU,
        memory: str | None = DEFAULT_TASK_QUEUE_MEMORY,
        gpu: str | None = None,
        gpu_count: int = 0,
        timeout: int | None = 3600,
        retries: int = 3,
        retry_for: Iterable[type[BaseException]] | None = None,
        retry_delay_seconds: float = 0.0,
        retry_policy: RetryPolicyInput = None,
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
        placement: PlacementInput = None,
        provider: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> Callable[[Callable[P, R]], TaskQueueFunction[P, R]]: ...

    def task_queue(
        self,
        func: Callable[P, R] | None = None,
        *,
        image: Image | None = None,
        name: str | None = None,
        cpu: float | None = DEFAULT_TASK_QUEUE_CPU,
        memory: str | None = DEFAULT_TASK_QUEUE_MEMORY,
        gpu: str | None = None,
        gpu_count: int = 0,
        timeout: int | None = 3600,
        retries: int = 3,
        retry_for: Iterable[type[BaseException]] | None = None,
        retry_delay_seconds: float = 0.0,
        retry_policy: RetryPolicyInput = None,
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
        placement: PlacementInput = None,
        provider: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> TaskQueueFunction[P, R] | Callable[[Callable[P, R]], TaskQueueFunction[P, R]]:
        """Register a background task queue function owned by this app.

        Use `@app.task_queue` for async background work submitted with
        `.put(...)` or `.put_many(...)`. The callable keeps its typed Python
        signature for local execution while deployed calls are routed through
        the queue service.

        Args:
            func: Callable to register when using `app.task_queue(fn)` directly.
            image: Image definition used to build or select the runtime image.
            name: Deployment resource name. Defaults to the callable name.
            cpu, memory, gpu, gpu_count: Compute resources requested per worker.
            timeout: Maximum runtime for one queued task.
            retries: Number of retry attempts for failed tasks.
            retry_for: Exception types that should trigger task retries.
            retry_delay_seconds: Delay before retrying a failed task.
            workers: Initial worker count for the queue.
            keep_warm_seconds: Seconds to keep idle workers available.
            max_pending_tasks: Queue backpressure limit for pending tasks.
            callback_url: Optional webhook called for task events.
            authorized: Whether queue submissions require authentication.
            env, secrets, volumes: Runtime configuration injected into workers.
            on_start and task lifecycle hooks: Hooks invoked by the workload runner.
            autoscaler, task_policy: Scheduling policies.
            inputs, outputs: Optional schema metadata for clients and validation.
            docker_enabled: Whether the execution container needs an isolated Docker daemon.
            pool, placement, provider, metadata: Compute placement and custom metadata.
        """
        kwargs = _task_queue_options(
            image=image,
            name=name,
            cpu=cpu,
            memory=memory,
            gpu=gpu,
            gpu_count=gpu_count,
            timeout=timeout,
            retries=retries,
            retry_for=retry_for,
            retry_delay_seconds=retry_delay_seconds,
            retry_policy=retry_policy,
            workers=workers,
            keep_warm_seconds=keep_warm_seconds,
            max_pending_tasks=max_pending_tasks,
            callback_url=callback_url,
            authorized=authorized,
            env=env,
            secrets=secrets,
            volumes=volumes,
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
            placement=placement,
            provider=provider,
            metadata=metadata,
        )

        def decorate(target: Callable[P, R]) -> TaskQueueFunction[P, R]:
            return self._register(task_queue_decorator(target, _app_slug=self.slug, **kwargs))

        return decorate if func is None else decorate(func)

    def pod(
        self,
        *,
        name: str = "pod",
        image: Image | None = None,
        command: Iterable[str] | None = None,
        ports: Mapping[str, int] | None = None,
        env: Mapping[str, str] | None = None,
        cpu: float | None = 1.0,
        memory: str | None = "128Mi",
        gpu: str | None = None,
        gpu_count: int = 0,
        keep_warm: int = 600,
        secrets: Iterable[str] | None = None,
        volumes: Iterable[VolumeMount | VolumeExport] | None = None,
        authorized: bool = False,
        checkpoint_enabled: bool = False,
        checkpoint_readiness_path: str | None = None,
        checkpoint_readiness_port: int | None = None,
        checkpoint_readiness_timeout_seconds: int = 600,
        checkpoint_readiness_interval_seconds: float = 1.0,
        tcp: bool = False,
        block_network: bool = False,
        allow_list: list[str] | None = None,
        docker_enabled: bool = False,
        preemptible: bool = False,
        pool: PoolInput = None,
        placement: PlacementInput = None,
        provider: str | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> Pod:
        """Create an app-owned long-running pod resource.

        Pods are for command-style containers that should be created, deployed,
        and managed under the same app slug as the rest of the resources.

        Args:
            name: Deployment resource name for the pod.
            image: Image definition used to build or select the pod image.
            command: Container command and arguments.
            ports: Named ports exposed by the pod.
            env: Environment variables injected into the pod.
            cpu, memory, gpu, gpu_count: Compute resources requested for the pod.
            keep_warm: Seconds to keep the pod alive when idle.
            secrets: Secret names mounted into the pod environment.
            volumes: Durable volumes mounted into the pod.
            authorized: Whether ingress requires an authenticated client.
            checkpoint_enabled: Enable checkpoint support when available.
            checkpoint_readiness_path, checkpoint_readiness_port: HTTP readiness probe used
                before checkpointing a Pod without a managed runner.
            tcp, block_network, allow_list, docker_enabled: Network and Docker policy.
            pool, provider, metadata: Placement and custom metadata.
        """
        kwargs = _pod_options(
            name=name,
            image=Image() if image is None else image,
            command=[str(item) for item in (command or [])],
            ports=dict(ports or {}),
            env=dict(env or {}),
            cpu=cpu,
            memory=memory,
            gpu=gpu,
            gpu_count=gpu_count,
            keep_warm=keep_warm,
            secrets=[str(secret) for secret in (secrets or [])],
            volumes=volume_mounts(volumes or ()),
            authorized=authorized,
            checkpoint_enabled=checkpoint_enabled,
            checkpoint_readiness_path=checkpoint_readiness_path,
            checkpoint_readiness_port=checkpoint_readiness_port,
            checkpoint_readiness_timeout_seconds=checkpoint_readiness_timeout_seconds,
            checkpoint_readiness_interval_seconds=checkpoint_readiness_interval_seconds,
            tcp=tcp,
            block_network=block_network,
            allow_list=allow_list,
            docker_enabled=docker_enabled,
            preemptible=preemptible,
            pool=pool,
            placement=placement,
            provider=provider,
            metadata=dict(metadata or {}),
        )
        return self._register(Pod(_app_slug=self.slug, **kwargs))

    def sandbox(
        self,
        *,
        cpu: int | float | str = 1.0,
        memory: int | str = 128,
        gpu: str | None = None,
        gpu_count: int = 0,
        image: Image | None = None,
        keep_warm_seconds: int = 600,
        authorized: bool = False,
        name: str | None = None,
        volumes: Iterable[VolumeMount | VolumeExport] | None = None,
        secrets: Iterable[str] | None = None,
        env: Mapping[str, str] | None = None,
        sync_local_dir: bool = False,
        block_network: bool = False,
        allow_list: Iterable[str] | None = None,
        docker_enabled: bool = False,
        preemptible: bool = False,
        ports: Iterable[int] | None = None,
        pool: PoolInput = None,
        placement: PlacementInput = None,
        provider: str | None = None,
        metadata: Mapping[str, Any] | None = None,
        command: Iterable[str] | None = None,
    ) -> Sandbox:
        """Create an app-owned interactive sandbox resource.

        Sandboxes are on-demand containers for commands, filesystem operations,
        exposed ports, and process control. They are registered under the app
        slug so client handles and deployments can resolve them consistently.

        Args:
            cpu, memory, gpu, gpu_count: Compute resources requested for the sandbox.
            image: Image definition used to build or select the sandbox image.
            keep_warm_seconds: Seconds to keep the sandbox alive when idle.
            authorized: Whether sandbox control requires authentication.
            name: Optional deployment resource name.
            volumes, secrets, env: Runtime configuration injected into the sandbox.
            sync_local_dir: Sync the local working directory into the sandbox.
            block_network, allow_list, docker_enabled: Network and Docker policy.
            ports: Container ports exposed from the sandbox.
            pool, provider, metadata: Placement and custom metadata.
            command: Optional initial command run by the sandbox container.
        """
        kwargs = _sandbox_options(
            cpu=cpu,
            memory=memory,
            gpu=gpu,
            gpu_count=gpu_count,
            image=image,
            keep_warm_seconds=keep_warm_seconds,
            authorized=authorized,
            name=name,
            volumes=volumes,
            secrets=secrets,
            env=env,
            sync_local_dir=sync_local_dir,
            block_network=block_network,
            allow_list=allow_list,
            docker_enabled=docker_enabled,
            preemptible=preemptible,
            ports=ports,
            pool=pool,
            placement=placement,
            provider=provider,
            metadata=metadata,
            command=command,
        )
        return self._register(Sandbox(_app_slug=self.slug, **kwargs))

    def deploy(
        self,
        *,
        resource: str | None = None,
        name: str | None = None,
        workspace: str | None = None,
        external_url: str | None = None,
        placement: PlacementInput = None,
        source_root: str | Path | None = None,
        image: Image | None = None,
        cpu: float | None = None,
        memory: str | None = None,
        gpu: str | None = None,
        gpu_count: int | None = None,
        env: Mapping[str, str] | None = None,
        secrets: Iterable[str] | None = None,
        ports: Mapping[str, int] | None = None,
        keep_warm: int | None = None,
        tcp: bool | None = None,
        pool: PoolInput = None,
        preemptible: bool | None = None,
        entrypoint: Iterable[str] | None = None,
    ) -> AppDeployResult:
        """Deploy this app or one selected app resource.

        Without `resource`, every deployable resource registered on the app is
        deployed. With `resource`, pass either the resource name or
        `"kind:name"` when names overlap, for example `"endpoint:api"`.

        Args:
            resource: Optional resource selector to deploy only one item.
            name: Deployment name override when deploying one resource.
            workspace: Workspace slug or name for the deployment.
            external_url: External URL to attach to endpoint-style deployments.
            placement: Compute placement applied to every selected deployable resource.
            source_root: Local source directory packaged for each selected resource.
            image, cpu, memory, gpu, gpu_count: Runtime overrides applied before deployment.
            env, secrets, ports, keep_warm, tcp, pool, entrypoint: Additional runtime
                overrides. Target-specific options fail explicitly when unsupported.
        """
        deployable = self._select_many(resource=resource, method="deploy")
        for item in deployable:
            _configure_deployable_resource(
                item,
                image=image,
                cpu=cpu,
                memory=memory,
                gpu=gpu,
                gpu_count=gpu_count,
                env=env,
                secrets=secrets,
                ports=ports,
                keep_warm=keep_warm,
                tcp=tcp,
                pool=pool,
                placement=placement,
                preemptible=preemptible,
                entrypoint=entrypoint,
            )
        results: list[object] = []
        for item in deployable:
            method = item.deploy
            method_kwargs: dict[str, object] = {
                "name": name if resource else None,
                "workspace": workspace,
                "external_url": external_url,
                "source_root": source_root,
            }
            results.append(_invoke_method(method, method_kwargs))
        return AppDeployResult(app=self.slug, resources=tuple(results))

    def serve(
        self,
        *,
        resource: str | None = None,
        timeout: int = 0,
        url_type: str = "",
    ) -> object:
        """Serve one endpoint, ASGI app, or task queue resource for preview.

        Use `resource` when an app contains more than one serveable resource.
        Select by name or by `"kind:name"`, such as `"task-queue:summarize"`.

        Args:
            resource: Optional resource selector for the preview target.
            timeout: Serve timeout in seconds. `0` keeps serving until stopped.
            url_type: Optional URL type requested from the gateway.
        """
        selected = self._select_one(resource=resource, method="serve", serveable=True)
        return _invoke_method(selected.serve, {"timeout": timeout, "url_type": url_type})

    def _register(self, resource: ResourceT) -> ResourceT:
        if not hasattr(resource, "spec"):
            msg = "app resources must provide spec()"
            raise AppOperationError(msg)
        spec = resource.spec()
        key = (spec.kind, spec.name)
        existing = self._resources.get(key)
        if existing is not None and existing is not resource:
            msg = f"duplicate {spec.kind.value} resource in app {self.slug}: {spec.name}"
            raise AppOperationError(msg)
        self._resources[key] = resource
        return resource

    def _select_many(self, *, resource: str | None, method: str) -> tuple[Any, ...]:
        if resource:
            return (self._select_one(resource=resource, method=method),)
        selected = tuple(item for item in self.resources if callable(getattr(item, method, None)))
        if not selected:
            msg = f"app {self.slug} has no resources that support {method}"
            raise AppOperationError(msg)
        return selected

    def _select_one(
        self,
        *,
        resource: str | None,
        method: str,
        serveable: bool = False,
    ) -> Any:
        candidates = [
            item
            for item in self._select_candidates(resource)
            if callable(getattr(item, method, None)) and (not serveable or _is_serveable(item))
        ]
        if len(candidates) == 1:
            return candidates[0]
        if not candidates:
            target = resource or f"app {self.slug}"
            msg = f"{target} has no resource that supports {method}"
            raise AppOperationError(msg)
        names = ", ".join(_resource_selector(item) for item in candidates)
        msg = f"select one resource with --resource; candidates: {names}"
        raise AppOperationError(msg)

    def _select_candidates(self, resource: str | None) -> Iterable[AppResource]:
        if resource is None:
            return self.resources
        kind, name = _parse_selector(resource)
        candidates: list[AppResource] = []
        for item in self.resources:
            spec = item.spec()
            if kind is not None and spec.kind is not kind:
                continue
            if spec.name == name:
                candidates.append(item)
        if not candidates:
            msg = f"resource not found in app {self.slug}: {resource}"
            raise AppOperationError(msg)
        return tuple(candidates)


def _parse_selector(value: str) -> tuple[DeploymentKind | None, str]:
    kind_text, separator, name = value.partition(":")
    if not separator:
        return None, value
    normalized = kind_text.replace("_", "-")
    try:
        return DeploymentKind(normalized), name
    except ValueError as exc:
        msg = f"unknown resource kind in selector: {kind_text}"
        raise AppOperationError(msg) from exc


def _resource_selector(resource: AppResource) -> str:
    spec = resource.spec()
    return f"{spec.kind.value}:{spec.name}"


def _configure_deployable_resource(
    resource: object,
    *,
    image: Image | None,
    cpu: float | None,
    memory: str | None,
    gpu: str | None,
    gpu_count: int | None,
    env: Mapping[str, str] | None,
    secrets: Iterable[str] | None,
    ports: Mapping[str, int] | None,
    keep_warm: int | None,
    tcp: bool | None,
    pool: PoolInput,
    placement: PlacementInput,
    preemptible: bool | None,
    entrypoint: Iterable[str] | None,
) -> None:
    if isinstance(resource, Function):
        unsupported = _unsupported_override_names(
            ports=ports,
            keep_warm=keep_warm,
            tcp=tcp,
            entrypoint=entrypoint,
        )
        if unsupported:
            spec = resource.spec()
            _raise_unsupported_overrides(spec, unsupported)
        resource.configure(
            image=image,
            cpu=cpu,
            memory=memory,
            gpu=gpu,
            gpu_count=gpu_count,
            env=env,
            secrets=secrets,
            pool=pool,
            placement=placement,
            preemptible=preemptible,
        )
        return
    if isinstance(resource, Endpoint):
        unsupported = _unsupported_override_names(
            ports=ports,
            tcp=tcp,
            entrypoint=entrypoint,
        )
        if unsupported:
            spec = resource.spec()
            _raise_unsupported_overrides(spec, unsupported)
        if image is not None:
            resource.image = image
        if cpu is not None:
            resource.cpu = cpu
        if memory is not None:
            resource.memory = memory
        if gpu is not None:
            resource.gpu = gpu
        if gpu_count is not None:
            resource.gpu_count = gpu_count
        if env:
            resource.env.update(env)
        if secrets:
            resource.secrets.extend(secret for secret in secrets if secret not in resource.secrets)
        if keep_warm is not None:
            resource.keep_warm = keep_warm
        if pool is not None:
            resource.pool = pool
        if placement is not None:
            resource.placement = placement
        if preemptible is not None:
            resource.preemptible = preemptible
        return
    if isinstance(resource, ASGI):
        unsupported = _unsupported_override_names(
            ports=ports,
            tcp=tcp,
            entrypoint=entrypoint,
        )
        if preemptible is not None:
            unsupported.append("preemptible")
        if unsupported:
            spec = resource.spec()
            _raise_unsupported_overrides(spec, unsupported)
        if image is not None:
            resource.image = image
        if cpu is not None:
            resource.cpu = cpu
        if memory is not None:
            resource.memory = memory
        if gpu is not None:
            resource.gpu = gpu
        if gpu_count is not None:
            resource.gpu_count = gpu_count
        if env:
            resource.env.update(env)
        if secrets:
            resource.secrets.extend(secret for secret in secrets if secret not in resource.secrets)
        if keep_warm is not None:
            resource.keep_warm_seconds = keep_warm
        if pool is not None:
            resource.pool = pool
        if placement is not None:
            resource.placement = placement
        return
    if isinstance(resource, TaskQueueFunction):
        unsupported = _unsupported_override_names(
            ports=ports,
            tcp=tcp,
            entrypoint=entrypoint,
        )
        if unsupported:
            spec = resource.spec()
            _raise_unsupported_overrides(spec, unsupported)
        if image is not None:
            resource.image = image
        if cpu is not None:
            resource.cpu = cpu
        if memory is not None:
            resource.memory = memory
        if gpu is not None:
            resource.gpu = gpu
        if gpu_count is not None:
            resource.gpu_count = gpu_count
        if env:
            resource.env.update(env)
        if secrets:
            resource.secrets.extend(secret for secret in secrets if secret not in resource.secrets)
        if keep_warm is not None:
            resource.keep_warm_seconds = keep_warm
        if pool is not None:
            resource.pool = pool
        if placement is not None:
            resource.placement = placement
        if preemptible is not None:
            resource.preemptible = preemptible
        return
    if isinstance(resource, Pod):
        resource.configure(
            image=image,
            command=list(entrypoint) if entrypoint is not None else None,
            ports=dict(ports) if ports is not None else None,
            env=dict(env) if env is not None else None,
            cpu=cpu,
            memory=memory,
            gpu=gpu,
            gpu_count=gpu_count,
            keep_warm=keep_warm,
            secrets=list(secrets) if secrets is not None else None,
            tcp=tcp,
            pool=pool,
            placement=placement,
            preemptible=preemptible,
        )
        return
    msg = "selected app resource does not support deployment overrides"
    raise AppOperationError(msg)


def _unsupported_override_names(
    *,
    ports: Mapping[str, int] | None = None,
    keep_warm: int | None = None,
    tcp: bool | None = None,
    entrypoint: Iterable[str] | None = None,
) -> list[str]:
    unsupported: list[str] = []
    if ports:
        unsupported.append("ports")
    if keep_warm is not None:
        unsupported.append("keep_warm")
    if tcp is not None:
        unsupported.append("tcp")
    if entrypoint:
        unsupported.append("entrypoint")
    return unsupported


def _raise_unsupported_overrides(spec: DeploymentSpec, unsupported: list[str]) -> None:
    options = ", ".join(unsupported)
    selector = f"{spec.kind.value}:{spec.name}"
    msg = f"{selector} does not support overrides: {options}"
    raise AppOperationError(msg)


def _is_serveable(resource: AppResource) -> bool:
    return resource.spec().kind in {
        DeploymentKind.Endpoint,
        DeploymentKind.Asgi,
        DeploymentKind.TaskQueue,
    }


def _dump_resource(value: object) -> object:
    if isinstance(value, ModelDumpable):
        return value.model_dump(mode="json")
    if is_dataclass(value) and not isinstance(value, type):
        return {
            field.name: getattr(value, field.name)
            for field in fields(value)
            if not field.name.startswith("_")
        }
    return value


def _invoke_method(method: Callable[..., Any], kwargs: Mapping[str, object]) -> Any:
    selected = {key: value for key, value in kwargs.items() if value is not None}
    try:
        signature = inspect.signature(method)
    except (TypeError, ValueError):
        return method(**selected)
    if any(
        parameter.kind is inspect.Parameter.VAR_KEYWORD
        for parameter in signature.parameters.values()
    ):
        return method(**selected)
    accepted = {key: value for key, value in selected.items() if key in signature.parameters}
    return method(**accepted)


def _function_options(
    *,
    image: Image | None,
    name: str | None,
    cpu: float | None,
    memory: str | None,
    gpu: str | None,
    gpu_count: int,
    timeout_seconds: int | None,
    retries: int,
    retry_policy: RetryPolicy | Mapping[str, Any] | None,
    retry_delay_seconds: float,
    callback_url: str | None,
    authorized: bool | None,
    env: dict[str, str] | None,
    secrets: list[str] | None,
    volumes: Iterable[VolumeMount | VolumeExport] | None,
    on_start: LifecycleHookInput,
    on_running: LifecycleHookInput,
    on_success: LifecycleHookInput,
    on_error: LifecycleHookInput,
    on_retry: LifecycleHookInput,
    on_failure: LifecycleHookInput,
    on_cancelled: LifecycleHookInput,
    on_timeout: LifecycleHookInput,
    on_finish: LifecycleHookInput,
    task_policy: TaskPolicy | Mapping[str, Any] | None,
    inputs: SchemaInput,
    outputs: SchemaInput,
    docker_enabled: bool,
    preemptible: bool,
    pool: PoolInput,
    placement: PlacementInput,
    provider: str | None,
    metadata: dict[str, Any] | None,
) -> FunctionOptions:
    return {
        "image": image,
        "name": name,
        "cpu": cpu,
        "memory": memory,
        "gpu": gpu,
        "gpu_count": gpu_count,
        "timeout_seconds": timeout_seconds,
        "retries": retries,
        "retry_policy": retry_policy,
        "retry_delay_seconds": retry_delay_seconds,
        "callback_url": callback_url,
        "authorized": authorized,
        "env": env,
        "secrets": secrets,
        "volumes": volumes,
        "on_start": on_start,
        "on_running": on_running,
        "on_success": on_success,
        "on_error": on_error,
        "on_retry": on_retry,
        "on_failure": on_failure,
        "on_cancelled": on_cancelled,
        "on_timeout": on_timeout,
        "on_finish": on_finish,
        "task_policy": task_policy,
        "inputs": inputs,
        "outputs": outputs,
        "docker_enabled": docker_enabled,
        "preemptible": preemptible,
        "pool": pool,
        "placement": placement,
        "provider": provider,
        "metadata": metadata,
    }


def _endpoint_options(
    *,
    image: Image | None,
    name: str | None,
    route: str,
    methods: list[str] | None,
    cpu: float | None,
    memory: str | None,
    gpu: str | None,
    gpu_count: int,
    timeout_seconds: int | None,
    retries: int,
    retry_policy: RetryPolicy | Mapping[str, Any] | None,
    retry_delay_seconds: float,
    workers: int,
    concurrency: int,
    keep_warm: int,
    max_pending_tasks: int | None,
    callback_url: str | None,
    authorized: bool | None,
    env: dict[str, str] | None,
    secrets: list[str] | None,
    volumes: Iterable[VolumeMount | VolumeExport] | None,
    on_start: LifecycleHookInput,
    autoscaler: QueueDepthAutoscaler | Mapping[str, Any] | None,
    task_policy: TaskPolicy | Mapping[str, Any] | None,
    checkpoint_enabled: bool,
    inputs: SchemaInput,
    outputs: SchemaInput,
    docker_enabled: bool,
    preemptible: bool,
    pool: PoolInput,
    placement: PlacementInput,
    provider: str | None,
    metadata: dict[str, Any] | None,
) -> EndpointOptions:
    return {
        "image": image,
        "name": name,
        "route": route,
        "methods": methods,
        "cpu": cpu,
        "memory": memory,
        "gpu": gpu,
        "gpu_count": gpu_count,
        "timeout_seconds": timeout_seconds,
        "retries": retries,
        "retry_policy": retry_policy,
        "retry_delay_seconds": retry_delay_seconds,
        "workers": workers,
        "concurrency": concurrency,
        "keep_warm": keep_warm,
        "max_pending_tasks": max_pending_tasks,
        "callback_url": callback_url,
        "authorized": authorized,
        "env": env,
        "secrets": secrets,
        "volumes": volumes,
        "on_start": on_start,
        "autoscaler": autoscaler,
        "task_policy": task_policy,
        "checkpoint_enabled": checkpoint_enabled,
        "inputs": inputs,
        "outputs": outputs,
        "docker_enabled": docker_enabled,
        "preemptible": preemptible,
        "pool": pool,
        "placement": placement,
        "provider": provider,
        "metadata": metadata,
    }


def _asgi_options(
    *,
    name: str,
    image: Image | None,
    route: str,
    cpu: float | None,
    memory: str | None,
    gpu: str | None,
    gpu_count: int,
    timeout_seconds: int | None,
    workers: int,
    concurrent_requests: int,
    keep_warm_seconds: int,
    max_pending_tasks: int,
    authorized: bool,
    callback_url: str | None,
    env: dict[str, str] | None,
    secrets: list[str] | None,
    volumes: Iterable[VolumeMount | VolumeExport] | None,
    on_start: LifecycleHookInput,
    autoscaler: QueueDepthAutoscaler | Mapping[str, Any] | None,
    task_policy: TaskPolicy | Mapping[str, Any] | None,
    checkpoint_enabled: bool,
    pool: PoolInput,
    placement: PlacementInput,
    provider: str | None,
) -> ASGIOptions:
    return {
        "name": name,
        "image": image,
        "route": route,
        "cpu": cpu,
        "memory": memory,
        "gpu": gpu,
        "gpu_count": gpu_count,
        "timeout_seconds": timeout_seconds,
        "workers": workers,
        "concurrent_requests": concurrent_requests,
        "keep_warm_seconds": keep_warm_seconds,
        "max_pending_tasks": max_pending_tasks,
        "authorized": authorized,
        "callback_url": callback_url,
        "env": env,
        "secrets": secrets,
        "volumes": volumes,
        "on_start": on_start,
        "autoscaler": autoscaler,
        "task_policy": task_policy,
        "checkpoint_enabled": checkpoint_enabled,
        "pool": pool,
        "placement": placement,
        "provider": provider,
    }


def _task_queue_options(
    *,
    image: Image | None,
    name: str | None,
    cpu: float | None,
    memory: str | None,
    gpu: str | None,
    gpu_count: int,
    timeout: int | None,
    retries: int,
    retry_for: Iterable[type[BaseException]] | None,
    retry_delay_seconds: float,
    retry_policy: RetryPolicy | Mapping[str, Any] | None,
    workers: int,
    keep_warm_seconds: int,
    max_pending_tasks: int,
    callback_url: str,
    authorized: bool,
    env: dict[str, str] | None,
    secrets: list[str] | None,
    volumes: Iterable[VolumeMount | VolumeExport] | None,
    on_start: LifecycleHookInput,
    on_running: LifecycleHookInput,
    on_success: LifecycleHookInput,
    on_error: LifecycleHookInput,
    on_retry: LifecycleHookInput,
    on_failure: LifecycleHookInput,
    on_cancelled: LifecycleHookInput,
    on_timeout: LifecycleHookInput,
    on_finish: LifecycleHookInput,
    autoscaler: QueueDepthAutoscaler | Mapping[str, Any] | None,
    task_policy: TaskPolicy | Mapping[str, Any] | None,
    checkpoint_enabled: bool,
    inputs: SchemaInput,
    outputs: SchemaInput,
    docker_enabled: bool,
    preemptible: bool,
    pool: PoolInput,
    placement: PlacementInput,
    provider: str | None,
    metadata: dict[str, Any] | None,
) -> TaskQueueOptions:
    return {
        "image": image,
        "name": name,
        "cpu": cpu,
        "memory": memory,
        "gpu": gpu,
        "gpu_count": gpu_count,
        "timeout": timeout,
        "retries": retries,
        "retry_for": retry_for,
        "retry_delay_seconds": retry_delay_seconds,
        "retry_policy": retry_policy,
        "workers": workers,
        "keep_warm_seconds": keep_warm_seconds,
        "max_pending_tasks": max_pending_tasks,
        "callback_url": callback_url,
        "authorized": authorized,
        "env": env,
        "secrets": secrets,
        "volumes": volumes,
        "on_start": on_start,
        "on_running": on_running,
        "on_success": on_success,
        "on_error": on_error,
        "on_retry": on_retry,
        "on_failure": on_failure,
        "on_cancelled": on_cancelled,
        "on_timeout": on_timeout,
        "on_finish": on_finish,
        "autoscaler": autoscaler,
        "task_policy": task_policy,
        "checkpoint_enabled": checkpoint_enabled,
        "inputs": inputs,
        "outputs": outputs,
        "docker_enabled": docker_enabled,
        "preemptible": preemptible,
        "pool": pool,
        "placement": placement,
        "provider": provider,
        "metadata": metadata,
    }


def _pod_options(
    *,
    name: str,
    image: Image,
    command: list[str],
    ports: dict[str, int],
    env: dict[str, str],
    cpu: float | None,
    memory: str | None,
    gpu: str | None,
    gpu_count: int,
    keep_warm: int,
    secrets: list[str],
    volumes: list[VolumeMount],
    authorized: bool,
    checkpoint_enabled: bool,
    checkpoint_readiness_path: str | None,
    checkpoint_readiness_port: int | None,
    checkpoint_readiness_timeout_seconds: int,
    checkpoint_readiness_interval_seconds: float,
    tcp: bool,
    block_network: bool,
    allow_list: list[str] | None,
    docker_enabled: bool,
    preemptible: bool,
    pool: PoolInput,
    placement: PlacementInput,
    provider: str | None,
    metadata: dict[str, Any],
) -> PodOptions:
    return {
        "name": name,
        "image": image,
        "command": command,
        "ports": ports,
        "env": env,
        "cpu": cpu,
        "memory": memory,
        "gpu": gpu,
        "gpu_count": gpu_count,
        "keep_warm": keep_warm,
        "secrets": secrets,
        "volumes": volumes,
        "authorized": authorized,
        "checkpoint_enabled": checkpoint_enabled,
        "checkpoint_readiness_path": checkpoint_readiness_path,
        "checkpoint_readiness_port": checkpoint_readiness_port,
        "checkpoint_readiness_timeout_seconds": checkpoint_readiness_timeout_seconds,
        "checkpoint_readiness_interval_seconds": checkpoint_readiness_interval_seconds,
        "tcp": tcp,
        "block_network": block_network,
        "allow_list": allow_list,
        "docker_enabled": docker_enabled,
        "preemptible": preemptible,
        "pool": pool,
        "placement": placement,
        "provider": provider,
        "metadata": metadata,
    }


def _sandbox_options(
    *,
    cpu: int | float | str,
    memory: int | str,
    gpu: str | None,
    gpu_count: int,
    image: Image | None,
    keep_warm_seconds: int,
    authorized: bool,
    name: str | None,
    volumes: Iterable[VolumeMount | VolumeExport] | None,
    secrets: Iterable[str] | None,
    env: Mapping[str, str] | None,
    sync_local_dir: bool,
    block_network: bool,
    allow_list: Iterable[str] | None,
    docker_enabled: bool,
    preemptible: bool,
    ports: Iterable[int] | None,
    pool: PoolInput,
    placement: PlacementInput,
    provider: str | None,
    metadata: Mapping[str, Any] | None,
    command: Iterable[str] | None,
) -> SandboxOptions:
    return {
        "cpu": cpu,
        "memory": memory,
        "gpu": gpu,
        "gpu_count": gpu_count,
        "image": image,
        "keep_warm_seconds": keep_warm_seconds,
        "authorized": authorized,
        "name": name,
        "volumes": volumes,
        "secrets": secrets,
        "env": env,
        "sync_local_dir": sync_local_dir,
        "block_network": block_network,
        "allow_list": allow_list,
        "docker_enabled": docker_enabled,
        "preemptible": preemptible,
        "ports": ports,
        "pool": pool,
        "placement": placement,
        "provider": provider,
        "metadata": metadata,
        "command": command,
    }


__all__ = ["App", "AppDeployResult", "AppOperationError"]
