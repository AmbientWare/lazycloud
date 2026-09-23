from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable, Iterable, Mapping
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextvars import copy_context
from dataclasses import dataclass
from pathlib import Path
from typing import Any, ParamSpec, Protocol, TypeVar, overload

from pydantic import JsonValue
from shared.app_slug import validate_app_slug
from shared.autoscaling import Autoscaler
from shared.deployment_records import (
    DEFAULT_FUNCTION_AUTHORIZED,
    DEFAULT_FUNCTION_CPU,
    DEFAULT_FUNCTION_MEMORY,
    DEFAULT_FUNCTION_RETRIES,
    DEFAULT_FUNCTION_TIMEOUT_SECONDS,
    DEFAULT_HTTP_CPU,
    DEFAULT_HTTP_MEMORY,
    DEFAULT_WORKLOAD_PREEMPTIBLE,
    CpuRequest,
    DeploymentSpec,
    MemoryRequest,
    VolumeMount,
)
from shared.deployments import DeploymentKind
from shared.disks import DiskMount
from shared.gpu import GpuInput
from shared.http.deployment_plans import (
    DeploymentPlanRequest,
    DeploymentPlanResponse,
    DeploymentPruneResponse,
    WorkloadIdentity,
)
from shared.http.errors import HttpApiError, HttpResponseDecodeError, HttpTransportError
from shared.http.gateway import DeployStubResponse
from shared.serialization import to_json_value
from shared.tasks import TaskPolicy

from lazycloud.abstractions.disk import Disk, disk_mounts
from lazycloud.abstractions.endpoint import (
    ASGI,
    ASGIOptions,
    Endpoint,
    EndpointOptions,
    RealtimeASGI,
)
from lazycloud.abstractions.endpoint import _asgi as asgi_decorator
from lazycloud.abstractions.endpoint import _endpoint as endpoint_decorator
from lazycloud.abstractions.function import Function, FunctionOptions
from lazycloud.abstractions.function import _function as function_decorator
from lazycloud.abstractions.image import Image
from lazycloud.abstractions.metadata import (
    LifecycleHookInput,
    MachineInput,
    RetryPolicyInput,
    SchemaInput,
)
from lazycloud.abstractions.pod import Pod, PodOptions
from lazycloud.abstractions.sandbox import Sandbox, SandboxOptions
from lazycloud.abstractions.volume import VolumeExport, volume_mounts
from lazycloud.control import resolve_control_client_config
from lazycloud.control_clients import resource_control_client
from lazycloud.json_contracts import resource_payload
from lazycloud.session.app_deployment import AppDeploymentSession, AppDeploymentTarget
from lazycloud.session.preparation import MAX_DEPLOYMENT_PREPARATIONS, DeploymentPreparation


class AppOperationError(RuntimeError):
    pass


class AppResource(Protocol):
    def spec(self) -> DeploymentSpec: ...


ResourceT = TypeVar("ResourceT", bound=AppResource)
P = ParamSpec("P")
R = TypeVar("R")


@dataclass(frozen=True, slots=True)
class AppDeployResult:
    app: str
    resources: tuple[DeployStubResponse, ...]
    pruning: DeploymentPruneResponse | None = None

    def model_dump(self, *, mode: str = "python") -> dict[str, JsonValue]:
        _ = mode
        payload: dict[str, JsonValue] = {
            "app": self.app,
            "resources": [to_json_value(resource_payload(item)) for item in self.resources],
        }
        if self.pruning is not None:
            payload["pruning"] = self.pruning.model_dump(mode="json")
        return payload


class App:
    """Owns the deployable resources registered under one application slug."""

    def __init__(self, slug: str) -> None:
        """Create an app namespace for functions, endpoints, pods, and sandboxes.

        The slug is the stable production identity used by deploy, serve, and
        generated client handles. Use a short lowercase slug such as
        `"billing"` or `"reporting_api"`.
        """
        self.slug = validate_app_slug(slug)
        self._resources: dict[tuple[DeploymentKind, str], AppResource] = {}

    @property
    def resources(self) -> tuple[AppResource, ...]:
        return tuple(self._resources.values())

    @classmethod
    def combine(cls, apps: Iterable[App]) -> tuple[App, ...]:
        combined: dict[str, App] = {}
        for app in apps:
            target = combined.setdefault(app.slug, cls(app.slug))
            for item in app.resources:
                target._register(item)
        return tuple(combined.values())

    def deployment_manifest(
        self, *, prune: bool = False, resource: str | None = None, name: str | None = None
    ) -> DeploymentPlanRequest:
        if prune and (resource is not None or name is not None):
            raise AppOperationError("pruning requires the complete app without a name override")
        selected = (
            self._select_many(resource=resource, method="deploy") if resource else self.resources
        )
        return DeploymentPlanRequest(
            app=self.slug,
            prune=prune,
            workloads=[
                WorkloadIdentity(kind=spec.kind, name=name or spec.name)
                for item in selected
                if callable(getattr(item, "deploy", None))
                for spec in [item.spec()]
            ],
        )

    def plan(self, *, prune: bool = False, workspace: str | None = None) -> DeploymentPlanResponse:
        config = resolve_control_client_config(workspace=workspace)
        try:
            return resource_control_client(config).plan_deployment(
                self.deployment_manifest(prune=prune)
            )
        except (HttpApiError, HttpTransportError, HttpResponseDecodeError) as exc:
            raise AppOperationError(str(exc)) from exc

    @overload
    def function(
        self,
        func: Callable[P, R],
        *,
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
    def function(
        self,
        func: None = None,
        *,
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

    def function(
        self,
        func: Callable[P, R] | None = None,
        *,
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
            concurrency: Invocations one container serves at once.
            in_process: Serve those invocations in one interpreter rather than
                one process each, so anything `on_start` loaded is loaded once.
                Needed to share a GPU; costs process isolation and true CPU
                parallelism.
            keep_warm: Idle seconds a container stays available for the next
                call, so a second call inside the window reaches an interpreter
                that has already imported the handler and run `on_start`. `0`
                retires it as soon as it goes idle.
            retries: Number of retry attempts for failed invocations.
            callback_url: Optional webhook called for execution events.
            authorized: Whether calls require an authenticated client.
            env, secrets, volumes: Runtime configuration injected into workers.
            on_start and task lifecycle hooks: Hooks invoked by the workload runner.
            task_policy: Scheduling policy for invocation retries and timeouts.
            inputs, outputs: Optional schema metadata for clients and validation.
            docker_enabled: Whether the execution container needs an isolated Docker daemon.
            machine, metadata: A joined machine this workload must run on, by name
                (unset runs in the workspace), and custom metadata.
        """
        kwargs = FunctionOptions(
            image=image,
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
            env=env,
            secrets=secrets,
            volumes=volumes,
            on_start=on_start,
            on_running=on_running,
            on_success=on_success,
            on_error=on_error,
            on_retry=on_retry,
            on_failure=on_failure,
            on_finish=on_finish,
            task_policy=task_policy,
            inputs=inputs,
            outputs=outputs,
            docker_enabled=docker_enabled,
            preemptible=preemptible,
            region=region,
            availability_zone=availability_zone,
            machine=machine,
            metadata=metadata,
        )

        def decorate(target: Callable[P, R]) -> Function[P, R]:
            return self._register(function_decorator(target, _app_slug=self.slug, **kwargs))

        return decorate if func is None else decorate(func)

    @overload
    def endpoint(
        self,
        func: Callable[P, R],
        *,
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
    def endpoint(
        self,
        func: None = None,
        *,
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

    def endpoint(
        self,
        func: Callable[P, R] | None = None,
        *,
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
            machine, metadata: A joined machine this workload must run on, by name
                (unset runs in the workspace), and custom metadata.
        """
        kwargs = EndpointOptions(
            image=image,
            name=name,
            route=route,
            domain=domain,
            methods=methods,
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
            region=region,
            availability_zone=availability_zone,
            machine=machine,
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
            machine: A joined machine this workload must run on, by name; unset runs
                in the workspace.
        """
        kwargs = ASGIOptions(
            name=name,
            image=image,
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
            env=env,
            secrets=secrets,
            volumes=volumes,
            on_start=on_start,
            autoscaler=autoscaler,
            task_policy=task_policy,
            checkpoint_enabled=checkpoint_enabled,
            preemptible=preemptible,
            region=region,
            availability_zone=availability_zone,
            machine=machine,
        )
        factory = asgi_decorator(resource_type=ASGI, _app_slug=self.slug, **kwargs)

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
            machine: A joined machine this workload must run on, by name; unset runs
                in the workspace.
        """
        kwargs = ASGIOptions(
            name=name,
            image=image,
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
            env=env,
            secrets=secrets,
            volumes=volumes,
            on_start=on_start,
            autoscaler=autoscaler,
            task_policy=task_policy,
            checkpoint_enabled=checkpoint_enabled,
            preemptible=preemptible,
            region=region,
            availability_zone=availability_zone,
            machine=machine,
        )
        factory = asgi_decorator(resource_type=RealtimeASGI, _app_slug=self.slug, **kwargs)

        def decorate(target: Callable[..., Any]) -> RealtimeASGI:
            return self._register(factory(target))

        return decorate

    def pod(
        self,
        *,
        name: str = "pod",
        image: Image | None = None,
        command: Iterable[str] | None = None,
        ports: Mapping[str, int] | None = None,
        env: Mapping[str, str] | None = None,
        cpu: CpuRequest | None = 1.0,
        memory: MemoryRequest | None = "128Mi",
        disk: str | None = None,
        gpu: GpuInput = None,
        gpu_count: int = 0,
        keep_warm: int = 600,
        secrets: Iterable[str] | None = None,
        volumes: Iterable[VolumeMount | VolumeExport] | None = None,
        disks: Iterable[Disk | DiskMount] | None = None,
        authorized: bool = False,
        checkpoint_enabled: bool = False,
        checkpoint_readiness_path: str | None = None,
        checkpoint_readiness_port: int | None = None,
        checkpoint_readiness_timeout_seconds: int = 600,
        checkpoint_readiness_interval_seconds: float = 1.0,
        health_check_path: str | None = None,
        health_check_port: int | None = None,
        tcp: bool = False,
        ssh: bool = False,
        block_network: bool = False,
        allow_list: list[str] | None = None,
        docker_enabled: bool = False,
        preemptible: bool = DEFAULT_WORKLOAD_PREEMPTIBLE,
        region: str | None = None,
        availability_zone: str = "",
        machine: MachineInput = None,
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
            disks: Durable disks the pod keeps across restarts; one at ``/`` holds
                the whole writable root. A pod with a disk runs one container.
            authorized: Whether ingress requires an authenticated client.
            checkpoint_enabled: Enable checkpoint support when available.
            checkpoint_readiness_path, checkpoint_readiness_port: HTTP readiness probe used
                before checkpointing a Pod without a managed runner.
            health_check_path, health_check_port: HTTP probe the proxy calls before routing
                to a container. Unset, it connects to the port instead, which proves only
                that something is listening.
            tcp, block_network, allow_list, docker_enabled: Network and Docker policy.
            ssh: Serve SSH through ``lazycloud ssh`` and ``lazycloud ssh-config``;
                an open SSH connection keeps the pod running.
            machine, metadata: A joined machine this workload must run on, by name
                (unset runs in the workspace), and custom metadata.
        """
        kwargs = PodOptions(
            name=name,
            image=Image() if image is None else image,
            command=[str(item) for item in (command or [])],
            ports=dict(ports or {}),
            env=dict(env or {}),
            cpu=cpu,
            memory=memory,
            disk=disk,
            gpu=gpu,
            gpu_count=gpu_count,
            keep_warm=keep_warm,
            secrets=[str(secret) for secret in (secrets or [])],
            volumes=volume_mounts(volumes or ()),
            disks=disk_mounts(disks or ()),
            authorized=authorized,
            checkpoint_enabled=checkpoint_enabled,
            checkpoint_readiness_path=checkpoint_readiness_path,
            checkpoint_readiness_port=checkpoint_readiness_port,
            checkpoint_readiness_timeout_seconds=checkpoint_readiness_timeout_seconds,
            checkpoint_readiness_interval_seconds=checkpoint_readiness_interval_seconds,
            health_check_path=health_check_path,
            health_check_port=health_check_port,
            tcp=tcp,
            ssh=ssh,
            block_network=block_network,
            allow_list=allow_list,
            docker_enabled=docker_enabled,
            preemptible=preemptible,
            region=region,
            availability_zone=availability_zone,
            machine=machine,
            metadata=dict(metadata or {}),
        )
        return self._register(Pod(_app_slug=self.slug, **kwargs))

    def sandbox(
        self,
        *,
        cpu: CpuRequest | str = 1.0,
        memory: MemoryRequest = 128,
        disk: str | None = None,
        gpu: GpuInput = None,
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
        preemptible: bool = DEFAULT_WORKLOAD_PREEMPTIBLE,
        ports: Iterable[int] | None = None,
        region: str | None = None,
        availability_zone: str = "",
        machine: MachineInput = None,
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
            machine, metadata: A joined machine this workload must run on, by name
                (unset runs in the workspace), and custom metadata.
            command: Optional initial command run by the sandbox container.
        """
        kwargs = SandboxOptions(
            cpu=cpu,
            memory=memory,
            disk=disk,
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
            region=region,
            availability_zone=availability_zone,
            machine=machine,
            metadata=metadata,
            command=command,
        )
        return self._register(Sandbox(_app_slug=self.slug, **kwargs))

    def deploy(
        self,
        *,
        prune: bool = False,
        resource: str | None = None,
        workspace: str | None = None,
        external_url: str | None = None,
        source_root: str | Path | None = None,
    ) -> AppDeployResult:
        """Deploy this app or one selected app resource.

        Without `resource`, every deployable resource registered on the app is
        deployed. With `resource`, pass either the resource name or
        `"kind:name"` when names overlap, for example `"endpoint:api"`.

        Up to four resources deploy concurrently and share matching source and
        image preparation within this call. Results retain registration order.
        On failure, queued deployments are canceled; running deployments may finish.

        Args:
            prune: Remove omitted workloads after all deployments register successfully.
                Requires a complete app. An empty app removes every deployed workload.
            resource: Optional resource selector to deploy only one item.
            workspace: Workspace slug or name for the deployment.
            external_url: External URL to attach to endpoint-style deployments.
            source_root: Local source directory shared by the selected resources.
        """
        if prune and resource is not None:
            raise AppOperationError("pruning requires the complete app without a resource selector")
        deployable = (
            self._select_many(resource=resource, method="deploy")
            if not prune or resource is not None or self.deployment_manifest().workloads
            else ()
        )

        def submit() -> tuple[DeployStubResponse, ...]:
            return self._submit_deployments(
                deployable,
                workspace=workspace,
                external_url=external_url,
                source_root=source_root,
            )

        if not prune:
            return AppDeployResult(app=self.slug, resources=submit())
        control = resource_control_client(
            resolve_control_client_config(workspace=workspace, timeout_seconds=60)
        )
        try:
            outcome = AppDeploymentSession(control).deploy(
                [
                    AppDeploymentTarget(self.deployment_manifest(prune=True), submit),
                ]
            )[0]
        except (HttpApiError, HttpTransportError, HttpResponseDecodeError) as exc:
            raise AppOperationError(str(exc)) from exc
        return AppDeployResult(app=self.slug, resources=outcome.resources, pruning=outcome.pruning)

    def _submit_deployments(
        self,
        deployable: tuple[Function[..., Any] | Endpoint[..., Any] | ASGI | Pod, ...],
        *,
        workspace: str | None,
        external_url: str | None,
        source_root: str | Path | None,
    ) -> tuple[DeployStubResponse, ...]:
        with (
            DeploymentPreparation() as preparation,
            ThreadPoolExecutor(
                max_workers=MAX_DEPLOYMENT_PREPARATIONS, thread_name_prefix="app-deploy"
            ) as executor,
        ):
            submitted = [
                executor.submit(
                    copy_context().run,
                    _deploy_app_resource,
                    item,
                    {
                        "workspace": workspace,
                        "external_url": external_url,
                        "source_root": source_root,
                        "_preparation": preparation,
                    },
                )
                for item in deployable
            ]
            try:
                for future in as_completed(submitted):
                    future.result()
                results = tuple(future.result() for future in submitted)
            except BaseException:
                for future in submitted:
                    future.cancel()
                raise
        return results

    def serve(
        self,
        *,
        resource: str | None = None,
        timeout: int = 0,
        sync_dir: str | None = None,
    ) -> object:
        """Serve one function, endpoint, or ASGI app resource for preview.

        Use `resource` when an app contains more than one serveable resource.
        Select by name or by `"kind:name"`, such as `"endpoint:summarize"`.

        Args:
            resource: Optional resource selector for the preview target.
            timeout: Serve timeout in seconds. `0` keeps serving until stopped.
            sync_dir: Local directory synced into the preview. Defaults to the
                resource's own setting, then the current directory.
        """
        selected = self._select_one(resource=resource, method="serve", serveable=True)
        return _invoke_method(selected.serve, {"timeout": timeout, "sync_dir": sync_dir})

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
        msg = f"select a workload directly; candidates: {names}"
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


def _is_serveable(resource: AppResource) -> bool:
    return resource.spec().kind in {
        DeploymentKind.Function,
        DeploymentKind.Endpoint,
        DeploymentKind.Asgi,
    }


def _deploy_app_resource(
    item: Function[..., Any] | Endpoint[..., Any] | ASGI | Pod,
    kwargs: Mapping[str, object],
) -> DeployStubResponse:
    try:
        result = _invoke_method(item.deploy, kwargs)
        if not isinstance(result, DeployStubResponse):
            raise AppOperationError("deployment did not return its registered deployment")
        return result
    except RuntimeError as exc:
        spec = item.spec()
        raise AppOperationError(f"failed to deploy {spec.kind.value}:{spec.name}: {exc}") from exc


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


__all__ = ["App", "AppDeployResult", "AppOperationError"]
