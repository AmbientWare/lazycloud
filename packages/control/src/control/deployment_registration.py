from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol

from database.records.apps import StubKind, StubRecord
from pydantic import JsonValue, TypeAdapter
from shared.app_slug import app_slug_or_default
from shared.autoscaling import QueueDepthAutoscaler
from shared.deployment_records import (
    Deployment,
    DeploymentSpec,
    VolumeMount,
    resolve_authorized,
    resolve_cpu,
    resolve_disk,
    resolve_keep_warm_seconds,
    resolve_max_pending_tasks,
    resolve_memory,
    resolve_retries,
    resolve_timeout_seconds,
)
from shared.deployments import DeploymentKind
from shared.errors import InvalidInputError, NotFoundError
from shared.workload_config import StubConfig

from control.apps import AppRegistry
from control.deployments import DeploymentAppResolution, DeploymentRegistration

_JSON_OBJECT_ADAPTER = TypeAdapter(dict[str, JsonValue])
_JSON_VALUE_ADAPTER: TypeAdapter[JsonValue] = TypeAdapter(JsonValue)


class DeploymentStubRegistry(Protocol):
    def create_stub(
        self,
        name: str,
        *,
        workspace: str = "default",
        kind: StubKind = StubKind.Function,
        handler: str | None = None,
        deployment_id: str | None = None,
        app_id: str | None = None,
        public: bool = False,
        config: StubConfig | Mapping[str, JsonValue] | None = None,
        metadata: Mapping[str, JsonValue] | None = None,
        reuse_existing: bool = True,
    ) -> StubRecord: ...

    def get_stub(
        self,
        stub_id_or_name: str,
        *,
        workspace: str | None = None,
    ) -> StubRecord: ...

    def discard_deployment_registration_stub(
        self,
        stub_id: str,
        *,
        deployment_id: str,
        workspace: str = "default",
    ) -> None: ...


@dataclass(frozen=True, slots=True)
class DeploymentRegistrationService:
    apps: AppRegistry
    stubs: DeploymentStubRegistry

    def resolve_deployment_app(
        self,
        spec: DeploymentSpec,
        *,
        workspace: str = "default",
    ) -> DeploymentAppResolution:
        source_stub = self._source_stub(spec, workspace=workspace)
        if source_stub is not None and source_stub.app_id:
            return DeploymentAppResolution(app_id=source_stub.app_id)
        metadata_app_id = spec.metadata.get("app_id")
        if isinstance(metadata_app_id, str) and metadata_app_id:
            app = self.apps.get(metadata_app_id, workspace=workspace)
            return DeploymentAppResolution(app_id=app.id)
        metadata_app = spec.metadata.get("app")
        app_name = app_slug_or_default(
            metadata_app if isinstance(metadata_app, str) else None,
            default=spec.name,
        )
        try:
            app = self.apps.get(app_name, workspace=workspace)
        except NotFoundError:
            return DeploymentAppResolution(app_id=None)
        return DeploymentAppResolution(app_id=app.id)

    def register_deployment(
        self,
        deployment: Deployment,
        *,
        workspace: str = "default",
    ) -> DeploymentRegistration:
        metadata_app = deployment.spec.metadata.get("app")
        source_stub = self._source_stub(deployment.spec, workspace=workspace)
        deployment_app = (
            self.apps.get(deployment.app_id, workspace=workspace) if deployment.app_id else None
        )
        source_app = (
            self.apps.get(source_stub.app_id, workspace=workspace)
            if source_stub is not None and source_stub.app_id
            else None
        )
        app_name_source = (
            metadata_app
            if isinstance(metadata_app, str)
            else deployment_app.name
            if deployment_app is not None
            else source_app.name
            if source_app is not None
            else None
        )
        app_name = app_slug_or_default(app_name_source, default=deployment.name)
        authorized = resolve_authorized(
            deployment.kind,
            _metadata_optional_bool(deployment.spec.metadata, "authorized"),
        )
        stub = self.stubs.create_stub(
            deployment.name,
            workspace=workspace,
            kind=StubKind(deployment.kind.value),
            handler=deployment.spec.handler,
            deployment_id=deployment.id,
            app_id=deployment.app_id or (source_stub.app_id if source_stub is not None else None),
            public=source_stub.public if source_stub is not None else not authorized,
            config=(
                source_stub.config.model_copy(deep=True)
                if source_stub is not None
                else _stub_config_from_deployment_spec(deployment.spec)
            ),
            metadata={
                **(source_stub.metadata if source_stub is not None else {}),
                "deployment_id": deployment.id,
            },
            reuse_existing=False,
        )
        try:
            app = self.apps.create(
                app_name,
                workspace=workspace,
                stub_id=stub.id,
                version=deployment.version,
                public=not authorized,
                metadata={
                    "deployment_id": deployment.id,
                    "deployment_kind": deployment.kind.value,
                },
            )
        except Exception as binding_failure:
            try:
                self.stubs.discard_deployment_registration_stub(
                    stub.id,
                    deployment_id=deployment.id,
                    workspace=workspace,
                )
            except Exception as cleanup_failure:
                raise ExceptionGroup(
                    "deployment app binding failed and stub cleanup was incomplete",
                    [binding_failure, cleanup_failure],
                ) from None
            raise
        return DeploymentRegistration(app_id=app.id, stub_id=stub.id)

    def _source_stub(
        self,
        spec: DeploymentSpec,
        *,
        workspace: str,
    ) -> StubRecord | None:
        source_stub_id = spec.metadata.get("stub_id")
        if not isinstance(source_stub_id, str) or not source_stub_id:
            return None
        try:
            return self.stubs.get_stub(source_stub_id, workspace=workspace)
        except NotFoundError:
            return None


def _stub_config_from_deployment_spec(spec: DeploymentSpec) -> StubConfig:
    image = spec.image
    resources = spec.resources
    metadata = _deployment_metadata(spec)
    pool = _deployment_pool(metadata)
    pool_selector = _pool_name(pool)
    return StubConfig.model_validate(
        {
            "object_id": "",
            "image": {
                "image_id": image.image_id,
                "python_version": image.python_version,
                "base": image.base,
                "packages": list(image.packages),
                "commands": list(image.commands),
                "build_steps": [step.model_dump(mode="json") for step in image.build_steps],
                "env": dict(image.env),
                "workdir": image.workdir,
                "dockerfile": image.dockerfile,
                "context_path": image.context_path,
                "context_digest": image.context_digest,
                "context_object_id": image.context_object_id,
                "include_files_patterns": list(image.include_files_patterns),
                "credential_keys": list(image.credential_keys),
                "secrets": list(image.secrets),
                "gpu": image.gpu,
                "ignore_python": image.ignore_python,
                "entrypoint": list(spec.command),
            },
            "runtime": {
                "cpu": resolve_cpu(spec.kind, resources.cpu),
                "memory": resolve_memory(spec.kind, resources.memory),
                "disk": resolve_disk(resources.disk),
                "gpu": resources.gpu,
                "gpu_count": resources.gpu_count,
                "timeout_seconds": resolve_timeout_seconds(spec.kind, resources.timeout_seconds)
                or 0,
                "retries": resolve_retries(
                    spec.kind,
                    spec.retry_policy.retry_count if spec.retry_policy is not None else None,
                ),
                "keep_warm": resolve_keep_warm_seconds(spec.kind, resources.keep_warm),
                "concurrency": resources.concurrency,
                "checkpoint_enabled": (
                    _metadata_optional_bool(metadata, "checkpoint_enabled") or False
                ),
                "checkpoint_readiness_path": _metadata_optional_string(
                    metadata,
                    "checkpoint_readiness_path",
                ),
                "checkpoint_readiness_port": (
                    _metadata_optional_int(metadata, "checkpoint_readiness_port") or 0
                ),
                "checkpoint_readiness_timeout_seconds": (
                    _metadata_optional_int(metadata, "checkpoint_readiness_timeout_seconds") or 600
                ),
                "checkpoint_readiness_interval_seconds": (
                    _metadata_optional_float(
                        metadata,
                        "checkpoint_readiness_interval_seconds",
                    )
                    or 1.0
                ),
                "docker_enabled": (_metadata_optional_bool(metadata, "docker_enabled") or False),
                "block_network": _metadata_optional_bool(metadata, "block_network") or False,
                "allow_list": _metadata_string_list(metadata, "allow_list"),
                "pool_selector": pool_selector,
            },
            "env": dict(spec.env),
            "route": spec.route,
            "methods": list(spec.methods),
            "command": list(spec.command),
            "ports": {str(name): port for name, port in spec.ports.items()},
            "volumes": [_volume_mount_config(volume) for volume in spec.volumes],
            "secrets": list(spec.secrets),
            "retry_policy": (
                spec.retry_policy.model_dump(mode="json") if spec.retry_policy is not None else None
            ),
            "metadata": metadata,
            "client_contract": (
                spec.client_contract.model_dump(mode="json") if spec.client_contract else None
            ),
            "lifecycle_hooks": spec.lifecycle_hooks.model_dump(mode="json"),
            "autoscaler": _deployment_autoscaler_config(spec),
            "callback_url": None,
            "max_pending_tasks": resolve_max_pending_tasks(
                spec.kind,
                _metadata_optional_int(metadata, "max_pending_tasks"),
            ),
            "extra": None,
            "schema": {
                "inputs": {},
                "outputs": {},
            },
            "tcp": _metadata_optional_bool(metadata, "tcp") or False,
            "pool": pool,
        }
    )


def _deployment_metadata(spec: DeploymentSpec) -> dict[str, JsonValue]:
    payload = _JSON_OBJECT_ADAPTER.validate_json(spec.model_dump_json(include={"metadata"}))
    metadata = payload.get("metadata")
    if not isinstance(metadata, dict):
        msg = "deployment metadata must be a JSON object"
        raise InvalidInputError(msg)
    return metadata


def _deployment_pool(metadata: Mapping[str, JsonValue]) -> dict[str, JsonValue]:
    pool = metadata.get("pool")
    if isinstance(pool, dict):
        return dict(pool)
    if isinstance(pool, str) and pool.strip():
        return {"name": pool.strip()}
    return {}


def _deployment_autoscaler_config(spec: DeploymentSpec) -> dict[str, JsonValue]:
    raw = _deployment_metadata(spec).get("autoscaler")
    if raw is None:
        max_containers = 1
        min_containers = 0
        tasks_per_container = _default_tasks_per_container(spec)
    else:
        configured = QueueDepthAutoscaler.model_validate(raw)
        max_containers = configured.max_containers
        min_containers = configured.min_containers
        tasks_per_container = configured.tasks_per_container
    if (
        spec.kind is DeploymentKind.Pod
        and resolve_keep_warm_seconds(spec.kind, spec.resources.keep_warm) == -1
    ):
        min_containers = max(min_containers, 1)
    return {
        "type": "queue_depth",
        "max_containers": max_containers,
        "min_containers": min_containers,
        "tasks_per_container": tasks_per_container,
    }


def _default_tasks_per_container(spec: DeploymentSpec) -> int:
    if spec.kind in {DeploymentKind.Endpoint, DeploymentKind.Asgi}:
        workers = _metadata_optional_int(spec.metadata, "workers")
        return max(workers if workers is not None else 1, 1) * spec.resources.concurrency
    if spec.kind is DeploymentKind.TaskQueue:
        return spec.resources.concurrency
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


def _metadata_optional_float(metadata: Mapping[str, JsonValue], key: str) -> float | None:
    value = metadata.get(key)
    if isinstance(value, bool) or value is None:
        return None
    if not isinstance(value, int | float | str):
        return None
    try:
        return float(value)
    except ValueError:
        return None


def _metadata_optional_string(metadata: Mapping[str, JsonValue], key: str) -> str:
    value = metadata.get(key)
    return value if isinstance(value, str) else ""


def _metadata_optional_bool(metadata: Mapping[str, JsonValue], key: str) -> bool | None:
    value = metadata.get(key)
    return value if isinstance(value, bool) else None


def _metadata_string_list(metadata: Mapping[str, JsonValue], key: str) -> list[str]:
    value = metadata.get(key)
    if value is None:
        return []
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        msg = f"metadata field {key!r} must be a list of strings"
        raise ValueError(msg)
    return [item for item in value if isinstance(item, str)]


def _pool_name(pool: Mapping[str, JsonValue]) -> str:
    value = pool.get("name")
    return value.strip() if isinstance(value, str) else ""


def _volume_mount_config(volume: VolumeMount) -> JsonValue:
    return _JSON_VALUE_ADAPTER.validate_json(volume.model_dump_json())
