from __future__ import annotations

from collections.abc import Mapping

from control.service import StubKind, StubRecord
from pydantic import JsonValue, TypeAdapter
from shared.autoscaling import QueueDepthAutoscaler
from shared.compute_policy import MachinePool
from shared.deployment_records import (
    DEFAULT_DISK,
    DeploymentSpec,
    Resources,
    VolumeMount,
    resolve_cpu,
    resolve_disk,
    resolve_keep_warm_seconds,
    resolve_memory,
    resolve_timeout_seconds,
)
from shared.deployments import DeploymentKind
from shared.http.gateway import GetOrCreateStubRequest
from shared.image_building.authoring import ImageBuildStep, ImageSpec
from shared.tasks import RetryPolicy
from shared.workload_config import (
    StubAutoscalerConfig,
    StubConfig,
    StubImageConfig,
    StubRuntimeConfig,
    StubSchemaConfig,
    StubTaskPolicy,
    StubVolumeConfig,
)

_JSON_OBJECT = TypeAdapter(dict[str, JsonValue])

STUB_KIND_ALIASES: dict[str, StubKind] = {
    "function": StubKind.Function,
    "endpoint": StubKind.Endpoint,
    "http": StubKind.Endpoint,
    "asgi": StubKind.Asgi,
    "pod": StubKind.Pod,
    "shell": StubKind.Shell,
    "sandbox": StubKind.Sandbox,
    "command": StubKind.Command,
    "cron": StubKind.CronJob,
    "cron-job": StubKind.CronJob,
}

DEPLOYABLE_STUB_KINDS: dict[StubKind, DeploymentKind] = {
    StubKind.Function: DeploymentKind.Function,
    StubKind.Endpoint: DeploymentKind.Endpoint,
    StubKind.Asgi: DeploymentKind.Asgi,
    StubKind.Pod: DeploymentKind.Pod,
    StubKind.Sandbox: DeploymentKind.Sandbox,
    StubKind.Command: DeploymentKind.Command,
    StubKind.CronJob: DeploymentKind.CronJob,
}


def stub_kind(value: str) -> StubKind:
    try:
        return STUB_KIND_ALIASES[value.lower()]
    except KeyError as exc:
        msg = f"invalid stub type: {value}"
        raise ValueError(msg) from exc


def stub_config(request: GetOrCreateStubRequest) -> StubConfig:
    metadata = _JSON_OBJECT.validate_python(request.metadata)
    env: dict[str, str | None] = {}
    for name, value in _env_dict(request.env).items():
        env[name] = value
    pool_selector = request.pool.strip()
    retry_policy = request.retry_policy or (
        RetryPolicy.from_retries(request.retries) if request.retries > 0 else None
    )
    return StubConfig(
        object_id=request.object_id,
        image=StubImageConfig(
            image_id=request.image_id or None,
            python_version=request.python_version,
            base=request.image_base,
            packages=request.python_packages,
            commands=request.image_commands,
            build_steps=[ImageBuildStep.model_validate(item) for item in request.image_build_steps],
            env=_env_dict(request.image_env),
            workdir=request.image_workdir,
            dockerfile=request.image_dockerfile or None,
            context_path=request.image_context_path or None,
            context_digest=request.image_context_digest or None,
            context_object_id=request.image_context_object or None,
            include_files_patterns=request.image_include_patterns,
            credential_keys=request.image_credential_keys,
            secrets=request.image_secrets,
            gpu=request.image_gpu or None,
            ignore_python=request.image_ignore_python,
            entrypoint=request.entrypoint,
        ),
        runtime=StubRuntimeConfig(
            cpu=resolve_cpu(request.stub_type, request.cpu),
            memory=resolve_memory(request.stub_type, request.memory),
            disk=resolve_disk(request.disk),
            gpu=request.gpu or None,
            gpu_count=request.gpu_count,
            timeout_seconds=resolve_timeout_seconds(
                request.stub_type,
                request.timeout or request.task_policy.timeout or None,
            )
            or 0,
            keep_warm=resolve_keep_warm_seconds(
                request.stub_type,
                request.keep_warm_seconds,
                min_containers=request.autoscaler.min_containers,
            ),
            concurrency=request.concurrent_requests,
            in_process=request.in_process,
            workers=request.workers,
            checkpoint_enabled=request.checkpoint_enabled,
            checkpoint_readiness_path=_metadata_string(
                metadata,
                "checkpoint_readiness_path",
                default="",
            ),
            checkpoint_readiness_port=_metadata_int(
                metadata,
                "checkpoint_readiness_port",
                default=0,
            ),
            checkpoint_readiness_timeout_seconds=_metadata_int(
                metadata,
                "checkpoint_readiness_timeout_seconds",
                default=600,
            ),
            checkpoint_readiness_interval_seconds=_metadata_float(
                metadata,
                "checkpoint_readiness_interval_seconds",
                default=1.0,
            ),
            docker_enabled=request.docker_enabled,
            block_network=request.block_network,
            allow_list=request.allow_list,
            pool_selector=pool_selector or None,
        ),
        env=env,
        route=request.route,
        domain=request.domain,
        methods=request.methods,
        command=request.command,
        ports={str(port): port for port in request.ports},
        volumes=[
            StubVolumeConfig.model_validate(item.model_dump(mode="python"))
            for item in request.volumes
        ],
        secrets=[item.name for item in request.secrets if item.name],
        metadata=metadata,
        retry_policy=retry_policy,
        client_contract=request.client_contract,
        lifecycle_hooks=request.lifecycle_hooks,
        autoscaler=_autoscaler_config(request),
        task_policy=StubTaskPolicy(
            timeout=request.task_policy.timeout,
            ttl=request.task_policy.ttl,
        ),
        callback_url=request.callback_url,
        max_pending_tasks=request.max_pending_tasks,
        extra=request.extra,
        schema_config=StubSchemaConfig(
            inputs=request.inputs.model_dump(mode="json"),
            outputs=request.outputs.model_dump(mode="json"),
        ),
        tcp=request.tcp,
        pool=MachinePool(pool_selector),
    )


def deployment_spec_from_stub(stub: StubRecord, *, name: str) -> DeploymentSpec:
    config = stub.config
    image_config = config.image
    runtime_config = config.runtime
    kind = DEPLOYABLE_STUB_KINDS.get(stub.kind, DeploymentKind.Function)
    return DeploymentSpec(
        name=name,
        kind=kind,
        handler=stub.handler,
        domain=config.domain,
        image=ImageSpec(
            base=image_config.base or "python:3.12-slim",
            image_id=image_config.image_id,
            python_version=image_config.python_version or "3.12",
            packages=image_config.packages,
            commands=image_config.commands,
            build_steps=image_config.build_steps,
            env=image_config.env,
            workdir=image_config.workdir or "/workspace",
            dockerfile=image_config.dockerfile,
            context_path=image_config.context_path,
            context_digest=image_config.context_digest,
            context_object_id=image_config.context_object_id,
            include_files_patterns=image_config.include_files_patterns,
            credential_keys=image_config.credential_keys,
            secrets=image_config.secrets,
            gpu=image_config.gpu,
            ignore_python=image_config.ignore_python,
        ),
        resources=Resources(
            cpu=float(runtime_config.cpu) if runtime_config.cpu is not None else None,
            memory=(
                str(runtime_config.memory) if runtime_config.memory not in {None, "", 0} else None
            ),
            # A stored config without a ceiling rehydrates to the platform one
            # rather than to None: the spec never carries an absent limit.
            disk=(
                str(runtime_config.disk)
                if runtime_config.disk not in {None, "", 0}
                else DEFAULT_DISK
            ),
            gpu=runtime_config.gpu,
            gpu_count=runtime_config.gpu_count,
            timeout_seconds=(
                int(runtime_config.timeout_seconds)
                if runtime_config.timeout_seconds is not None
                else None
            ),
            concurrency=runtime_config.concurrency,
            keep_warm=resolve_keep_warm_seconds(
                kind,
                runtime_config.keep_warm,
            ),
        ),
        env={key: value or "" for key, value in config.env.items()},
        secrets=config.secrets,
        volumes=[
            VolumeMount(
                name=volume.name or volume.id,
                mount_path=volume.mount_path,
                read_only=volume.read_only,
                config=(
                    volume.config.model_dump(mode="json", exclude_unset=True)
                    if volume.config
                    else None
                ),
            )
            for volume in config.volumes
        ],
        route=config.route,
        methods=config.methods or ["GET", "POST"],
        command=config.command,
        ports=config.ports,
        metadata={
            **config.metadata,
            "checkpoint_enabled": runtime_config.checkpoint_enabled,
            "checkpoint_readiness_path": runtime_config.checkpoint_readiness_path,
            "checkpoint_readiness_port": runtime_config.checkpoint_readiness_port,
            "checkpoint_readiness_timeout_seconds": (
                runtime_config.checkpoint_readiness_timeout_seconds
            ),
            "checkpoint_readiness_interval_seconds": (
                runtime_config.checkpoint_readiness_interval_seconds
            ),
            "schema": config.schema_config.model_dump(mode="json"),
            "autoscaler": QueueDepthAutoscaler(
                min_containers=config.autoscaler.min_containers,
                max_containers=config.autoscaler.max_containers,
                tasks_per_container=config.autoscaler.tasks_per_container,
            ).model_dump(mode="json"),
            "stub_id": stub.id,
            "stub_kind": stub.kind.value,
            "app_id": stub.app_id or "",
        },
        retry_policy=config.retry_policy,
        lifecycle_hooks=config.lifecycle_hooks,
        client_contract=config.client_contract,
    )


def _autoscaler_config(request: GetOrCreateStubRequest) -> StubAutoscalerConfig:
    autoscaler = StubAutoscalerConfig.model_validate(request.autoscaler.model_dump(mode="python"))
    kind = stub_kind(request.stub_type)
    updates: dict[str, int] = {}
    if "autoscaler" not in request.model_fields_set:
        updates["tasks_per_container"] = _default_tasks_per_container(request, kind)
    keep_warm_seconds = resolve_keep_warm_seconds(
        request.stub_type,
        request.keep_warm_seconds,
    )
    if kind is StubKind.Pod and keep_warm_seconds == -1:
        updates["min_containers"] = max(autoscaler.min_containers, 1)
    return autoscaler.model_copy(update=updates)


def _default_tasks_per_container(
    request: GetOrCreateStubRequest,
    kind: StubKind,
) -> int:
    if kind in {StubKind.Endpoint, StubKind.Asgi}:
        return max(request.workers, 1) * request.concurrent_requests
    return 1


def _env_dict(values: list[str]) -> dict[str, str]:
    result: dict[str, str] = {}
    for value in values:
        name, separator, raw = value.partition("=")
        if name:
            result[name] = raw if separator else ""
    return result


def _metadata_string(
    metadata: Mapping[str, JsonValue],
    key: str,
    *,
    default: str,
) -> str:
    value = metadata.get(key)
    if value is None:
        return default
    if not isinstance(value, str):
        msg = f"{key} must be a string"
        raise ValueError(msg)
    return value


def _metadata_int(
    metadata: Mapping[str, JsonValue],
    key: str,
    *,
    default: int,
) -> int:
    value = metadata.get(key)
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int):
        msg = f"{key} must be an integer"
        raise ValueError(msg)
    return value


def _metadata_float(
    metadata: Mapping[str, JsonValue],
    key: str,
    *,
    default: float,
) -> float:
    value = metadata.get(key)
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int | float):
        msg = f"{key} must be a number"
        raise ValueError(msg)
    return float(value)


__all__ = ["deployment_spec_from_stub", "stub_config", "stub_kind"]
