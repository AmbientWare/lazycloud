from __future__ import annotations

from collections.abc import Mapping

from database.records.apps import StubRecord
from pydantic import JsonValue
from shared.autoscaling import Autoscaler
from shared.deployment_records import (
    DEFAULT_DISK,
    DeploymentSpec,
    MemoryRequest,
    Resources,
    VolumeMount,
    declared_min_containers,
    request_and_limit,
    resolve_authorized,
    resolve_cpu,
    resolve_keep_warm_seconds,
    resolve_max_pending_tasks,
    resolve_memory,
    resolve_pod_command,
    resolve_pod_disks,
    resolve_pod_role,
    resolve_pod_ssh,
    resolve_preemptible,
    resolve_retries,
    resolve_timeout_seconds,
)
from shared.deployments import DeploymentKind, PodRole
from shared.errors import InvalidInputError
from shared.image_building.authoring import ImageSpec
from shared.image_building.constants import DEFAULT_IMAGE_BASE
from shared.tasks import RetryPolicy
from shared.workload_config import (
    StubAutoscalerConfig,
    StubConfig,
    StubImageConfig,
    StubRuntimeConfig,
    StubVolumeConfig,
)

from control.tcp_ingress import require_tcp_ingress


def normalize_deployment_spec(spec: DeploymentSpec) -> DeploymentSpec:
    default_retries = resolve_retries(spec.kind, None)
    metadata = dict(spec.metadata)
    role = resolve_pod_role(spec.kind, spec.role)
    if role is not None:
        ssh = metadata.get("ssh")
        metadata["ssh"] = resolve_pod_ssh(role, ssh if isinstance(ssh, bool) else None)
    if "authorized" not in metadata:
        metadata["authorized"] = resolve_authorized(spec.kind, None)
    if metadata.get("tcp") is True:
        if spec.kind is not DeploymentKind.Pod or not spec.ports:
            raise InvalidInputError("raw TCP ingress requires a Pod with an exposed port")
        require_tcp_ingress(public=metadata["authorized"] is False)
    if "max_pending_tasks" not in metadata:
        max_pending_tasks = resolve_max_pending_tasks(spec.kind, None)
        if max_pending_tasks is not None:
            metadata["max_pending_tasks"] = max_pending_tasks
    return spec.model_copy(
        update={
            "resources": spec.resources.model_copy(
                update={
                    "cpu": resolve_cpu(spec.kind, spec.resources.cpu),
                    "memory": resolve_memory(spec.kind, spec.resources.memory),
                    "timeout_seconds": resolve_timeout_seconds(
                        spec.kind,
                        spec.resources.timeout_seconds,
                    ),
                    "keep_warm": resolve_keep_warm_seconds(
                        spec.kind,
                        spec.resources.keep_warm,
                        min_containers=declared_min_containers(spec.metadata),
                        scheduled=bool(spec.cron),
                        role=role,
                    ),
                    "preemptible": resolve_preemptible(role, spec.resources.preemptible),
                }
            ),
            "role": role,
            "command": resolve_pod_command(role, spec.command),
            "disks": resolve_pod_disks(
                role,
                name=spec.name,
                disks=spec.disks,
                root_disk_bytes=spec.root_disk_bytes,
            ),
            "root_disk_bytes": None,
            "retry_policy": (
                spec.retry_policy
                if spec.retry_policy is not None
                else RetryPolicy.from_retries(default_retries)
                if default_retries > 0
                else None
            ),
            "metadata": metadata,
        }
    )


def deployment_stub_config(spec: DeploymentSpec) -> StubConfig:
    resources = spec.resources
    metadata = spec.metadata
    runtime_options = {
        key: metadata[key]
        for key in (
            "in_process",
            "workers",
            "checkpoint_enabled",
            "checkpoint_readiness_path",
            "checkpoint_readiness_port",
            "checkpoint_readiness_timeout_seconds",
            "checkpoint_readiness_interval_seconds",
            "health_check_path",
            "health_check_port",
            "docker_enabled",
            "block_network",
            "allow_list",
        )
        if metadata.get(key) is not None
    }
    try:
        runtime = StubRuntimeConfig.model_validate(
            {
                **runtime_options,
                **resources.model_dump(),
                "timeout_seconds": resources.timeout_seconds or 0,
                "retries": spec.retry_policy.retry_count if spec.retry_policy else 0,
            }
        )
        return StubConfig(
            image=StubImageConfig.model_validate(spec.image, from_attributes=True).model_copy(
                update={"entrypoint": list(spec.command)}
            ),
            runtime=runtime,
            env=dict(spec.env),
            route=spec.route,
            domain=spec.domain,
            methods=list(spec.methods),
            cron=spec.cron,
            command=list(spec.command),
            ports={str(name): port for name, port in spec.ports.items()},
            volumes=[
                StubVolumeConfig.model_validate(volume, from_attributes=True)
                for volume in spec.volumes
            ],
            disks=list(spec.disks),
            secrets=list(spec.secrets),
            retry_policy=spec.retry_policy,
            metadata=dict(metadata),
            client_contract=spec.client_contract,
            lifecycle_hooks=spec.lifecycle_hooks,
            autoscaler=_deployment_autoscaler_config(spec),
            max_pending_tasks=_metadata_optional_int(metadata, "max_pending_tasks"),
            tcp=metadata.get("tcp") is True,
            ssh=metadata.get("ssh") is True,
            role=spec.role or PodRole.Service,
        )
    except ValueError as exc:
        raise InvalidInputError(str(exc)) from exc


def _deployment_autoscaler_config(spec: DeploymentSpec) -> StubAutoscalerConfig:
    raw = spec.metadata.get("autoscaler")
    configured = (
        Autoscaler.model_validate(raw)
        if raw is not None
        else Autoscaler(tasks_per_container=_default_tasks_per_container(spec))
    )
    if (
        spec.kind is DeploymentKind.Pod
        and resolve_keep_warm_seconds(spec.kind, spec.resources.keep_warm) == -1
    ):
        configured.min_containers = max(configured.min_containers, 1)
    return StubAutoscalerConfig.model_validate(configured, from_attributes=True)


def _default_tasks_per_container(spec: DeploymentSpec) -> int:
    if spec.kind in {DeploymentKind.Endpoint, DeploymentKind.Asgi}:
        workers = _metadata_optional_int(spec.metadata, "workers")
        return max(workers if workers is not None else 1, 1) * spec.resources.concurrency
    return 1


def _metadata_optional_int(metadata: Mapping[str, JsonValue], key: str) -> int | None:
    value = metadata.get(key)
    if isinstance(value, bool) or value is None:
        return None
    if not isinstance(value, int | float | str):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def deployment_spec_from_stub(stub: StubRecord, *, name: str) -> DeploymentSpec:
    config = stub.config
    image_config = config.image
    runtime_config = config.runtime
    kind = DeploymentKind(stub.kind.value)
    return DeploymentSpec(
        name=name,
        kind=kind,
        role=config.role if kind is DeploymentKind.Pod else None,
        handler=stub.handler,
        domain=config.domain,
        image=ImageSpec.model_validate(image_config, from_attributes=True).model_copy(
            update={
                "base": image_config.base or DEFAULT_IMAGE_BASE,
                "python_version": image_config.python_version or "3.12",
                "workdir": image_config.workdir or "/workspace",
            }
        ),
        resources=Resources.model_validate(
            {
                **runtime_config.model_dump(include=set(Resources.model_fields)),
                "memory": runtime_config.memory if _memory_stated(runtime_config.memory) else None,
                "disk": str(runtime_config.disk)
                if runtime_config.disk not in {None, "", 0}
                else DEFAULT_DISK,
                "timeout_seconds": int(runtime_config.timeout_seconds)
                if runtime_config.timeout_seconds is not None
                else None,
                "keep_warm": resolve_keep_warm_seconds(kind, runtime_config.keep_warm),
            }
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
        disks=config.disks,
        route=config.route,
        methods=config.methods or ["GET", "POST"],
        cron=config.cron,
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
            "health_check_path": runtime_config.health_check_path,
            "health_check_port": runtime_config.health_check_port,
            "schema": config.schema_config.model_dump(mode="json"),
            "autoscaler": Autoscaler(
                min_containers=config.autoscaler.min_containers,
                max_containers=config.autoscaler.max_containers,
                tasks_per_container=config.autoscaler.tasks_per_container,
            ).model_dump(mode="json"),
            "stub_id": stub.id,
            "stub_kind": stub.kind.value,
            "app_id": stub.app_id or "",
            "machine": config.machine,
            "ssh": config.ssh,
        },
        retry_policy=config.retry_policy,
        lifecycle_hooks=config.lifecycle_hooks,
        client_contract=config.client_contract,
    )


def _memory_stated(value: MemoryRequest | None) -> bool:
    """Whether a stored memory value names anything at all."""
    request, _ = request_and_limit(value)
    return request is not None and request != "" and request != 0
