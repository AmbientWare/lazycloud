from __future__ import annotations

from database.records.apps import StubKind
from shared.deployment_records import (
    resolve_cpu,
    resolve_disk,
    resolve_keep_warm_seconds,
    resolve_memory,
    resolve_pod_command,
    resolve_pod_disks,
    resolve_pod_role,
    resolve_pod_ssh,
    resolve_preemptible,
    resolve_timeout_seconds,
)
from shared.deployments import PodRole
from shared.gpu import gpu_preference
from shared.http.gateway import GetOrCreateStubRequest
from shared.image_building.authoring import ImageBuildStep
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

STUB_KIND_ALIASES: dict[str, StubKind] = {
    "function": StubKind.Function,
    "endpoint": StubKind.Endpoint,
    "http": StubKind.Endpoint,
    "asgi": StubKind.Asgi,
    "pod": StubKind.Pod,
    "shell": StubKind.Shell,
    "sandbox": StubKind.Sandbox,
    "command": StubKind.Command,
}


def stub_kind(value: str) -> StubKind:
    try:
        return STUB_KIND_ALIASES[value.lower()]
    except KeyError as exc:
        msg = f"invalid stub type: {value}"
        raise ValueError(msg) from exc


def stub_config(request: GetOrCreateStubRequest) -> StubConfig:
    metadata = request.metadata
    readiness = StubRuntimeConfig.model_validate(
        {
            key: metadata[key]
            for key in (
                "checkpoint_readiness_path",
                "checkpoint_readiness_port",
                "checkpoint_readiness_timeout_seconds",
                "checkpoint_readiness_interval_seconds",
                "health_check_path",
                "health_check_port",
            )
            if metadata.get(key) is not None
        },
        strict=True,
    )
    retry_policy = request.retry_policy or (
        RetryPolicy.from_retries(request.retries) if request.retries > 0 else None
    )
    role = resolve_pod_role(request.stub_type, request.role)
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
            region=request.region,
            availability_zone=request.availability_zone,
            preemptible=resolve_preemptible(role, request.preemptible),
            cpu=resolve_cpu(request.stub_type, request.cpu),
            memory=resolve_memory(request.stub_type, request.memory),
            disk=resolve_disk(request.disk),
            gpu=list(gpu_preference(request.gpu)),
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
                scheduled=bool(request.cron),
                role=role,
            ),
            concurrency=request.concurrent_requests,
            in_process=request.in_process,
            workers=request.workers,
            checkpoint_enabled=request.checkpoint_enabled,
            checkpoint_readiness_path=readiness.checkpoint_readiness_path,
            checkpoint_readiness_port=readiness.checkpoint_readiness_port,
            checkpoint_readiness_timeout_seconds=readiness.checkpoint_readiness_timeout_seconds,
            checkpoint_readiness_interval_seconds=readiness.checkpoint_readiness_interval_seconds,
            health_check_path=readiness.health_check_path,
            health_check_port=readiness.health_check_port,
            docker_enabled=request.docker_enabled,
            block_network=request.block_network,
            allow_list=request.allow_list,
        ),
        env={name: value for name, value in _env_dict(request.env).items()},
        route=request.route,
        domain=request.domain,
        methods=request.methods,
        cron=request.cron or None,
        command=resolve_pod_command(role, request.command),
        ports={str(port): port for port in request.ports},
        volumes=[
            StubVolumeConfig.model_validate(item, from_attributes=True) for item in request.volumes
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
        ssh=resolve_pod_ssh(role, request.ssh),
        disks=resolve_pod_disks(
            role,
            name=request.name,
            disks=request.disks,
            root_disk_bytes=request.root_disk_bytes,
        ),
        role=role or PodRole.Service,
        machine=request.machine,
    )


def _autoscaler_config(request: GetOrCreateStubRequest) -> StubAutoscalerConfig:
    autoscaler = StubAutoscalerConfig.model_validate(request.autoscaler, from_attributes=True)
    kind = stub_kind(request.stub_type)
    updates: dict[str, int] = {}
    if "autoscaler" not in request.model_fields_set:
        updates["tasks_per_container"] = _default_tasks_per_container(request, kind)
    keep_warm_seconds = resolve_keep_warm_seconds(
        request.stub_type,
        request.keep_warm_seconds,
        role=resolve_pod_role(request.stub_type, request.role),
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


__all__ = ["stub_config", "stub_kind"]
