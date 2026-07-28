from __future__ import annotations

import hashlib
import json
import shutil
import tarfile
from collections.abc import Callable, Iterable
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol
from uuid import uuid4

from cache.protocol import CacheContentStoreResult
from pydantic import Field, JsonValue, TypeAdapter
from shared.checkpoints import CheckpointRecord
from shared.container_requests import WORKER_USER_ARTIFACT_VOLUME
from shared.contracts import ContractModel

from worker.checkpoint_activity import CheckpointLeaseRegistry
from worker.checkpoints import (
    CheckpointPersistencePlan,
    CheckpointPersistenceRequest,
    CheckpointRequest,
    CheckpointStatePayload,
    WorkerCheckpointStatus,
    build_checkpoint_plan,
    create_checkpoint_state_payload,
    plan_checkpoint_persistence,
)
from worker.container_client.models import ContainerArchiveResponse
from worker.container_service.models import WorkerContainerServiceInstance
from worker.execution import (
    CHECKPOINT_FILESYSTEM_DIR,
    checkpoint_accelerator,
    checkpoint_archive_path,
)
from worker.image_lifecycle import DEFAULT_IMAGE_ARCHIVE_EXTENSION, image_archive_source_key
from worker.oci_runtime import OCI_CONFIG_FILE_NAME
from worker.runtime_config import build_base_oci_config, runtime_capabilities

ARCHIVE_INITIAL_CONFIG_FILE_NAME = "initial_config.json"
# Derived from the mount constant rather than written out: a checkpoint that
# stops excluding the user artifact mount silently copies a task's saved files
# into the image, and a hand-written copy of the directory name is exactly what
# stops matching when the mount is renamed.
CHECKPOINT_COPY_EXCLUDES = frozenset(
    {OCI_CONFIG_FILE_NAME, WORKER_USER_ARTIFACT_VOLUME.strip("/"), "snapshot"}
)

type JsonObject = dict[str, JsonValue]

_JSON_OBJECT: TypeAdapter[JsonObject] = TypeAdapter(JsonObject)


class RuntimeCheckpointController(Protocol):
    def checkpoint_container(
        self,
        container_id: str,
        *,
        image_path: str,
        work_dir: str,
        leave_running: bool = True,
        allow_open_tcp: bool = True,
        skip_in_flight: bool = True,
        link_remap: bool = True,
    ) -> None: ...


class RuntimeStateController(Protocol):
    def status(self, container_id: str) -> str: ...


class CheckpointStateSink(Protocol):
    def save_checkpoint_state(self, payload: CheckpointStatePayload) -> CheckpointRecord: ...


class CheckpointArchiveUploader(Protocol):
    def upload_file(self, key: str, path: Path) -> None: ...


class CheckpointContentCacheStore(Protocol):
    def store_file(
        self,
        path: Path,
        *,
        cache_path: str,
        routing_key: str,
    ) -> CacheContentStoreResult: ...


class WorkerCheckpointPersister(Protocol):
    def persist_checkpoint(
        self,
        plan: CheckpointPersistencePlan,
    ) -> WorkerCheckpointPersistenceResult: ...


class ContainerImageArchiver(Protocol):
    def archive_image(
        self,
        source_path: Path,
        image_id: str,
        progress: Callable[[int], None],
    ) -> ContainerImageArchiveResult: ...


class ContainerImageArchivePublication(Protocol):
    @property
    def ok(self) -> bool: ...

    @property
    def error_message(self) -> str: ...


class ContainerImageArchivePublisher(Protocol):
    def publish_image_archive(
        self,
        *,
        image_id: str,
        archive_path: Path,
        workspace_id: str = "",
        stub_id: str = "",
    ) -> ContainerImageArchivePublication: ...


class WorkerCheckpointPersistenceResult(ContractModel):
    checkpoint_id: str
    archive_path: str
    origin_key: str
    cache_hash: str
    cache_size_bytes: int
    locality: str = ""
    accelerator: str = ""


class ContainerImageArchiveResult(ContractModel):
    success: bool
    archive_path: str = ""
    error_message: str = ""


class ContainerArchivePreparation(ContractModel):
    source_path: str
    initial_config_path: str
    runtime_config_path: str
    env: list[str] = Field(default_factory=list)


@dataclass(slots=True)
class FilesystemCheckpointPersister:
    uploader: CheckpointArchiveUploader
    cache_store: CheckpointContentCacheStore

    def persist_checkpoint(
        self,
        plan: CheckpointPersistencePlan,
    ) -> WorkerCheckpointPersistenceResult:
        if plan.error_message:
            raise RuntimeError(plan.error_message)
        archive_path = Path(plan.archive_path)
        checkpoint_path = Path(plan.checkpoint_path)
        if plan.remove_existing_archive:
            archive_path.unlink(missing_ok=True)
        archive_path.parent.mkdir(parents=True, exist_ok=True)
        _create_tar(checkpoint_path, archive_path, arcname=plan.checkpoint_id)
        cache_hash, size_bytes = _file_hash_and_size(archive_path)
        try:
            if plan.upload_to_origin_storage:
                self.uploader.upload_file(plan.origin_key, archive_path)
            if plan.store_archive_in_cache:
                self.cache_store.store_file(
                    archive_path,
                    cache_path=plan.origin_key,
                    routing_key=cache_hash,
                )
            return WorkerCheckpointPersistenceResult(
                checkpoint_id=plan.checkpoint_id,
                archive_path=str(archive_path),
                origin_key=plan.origin_key,
                cache_hash=cache_hash,
                cache_size_bytes=size_bytes,
                locality=plan.metadata.locality if plan.metadata else "",
                accelerator=plan.metadata.accelerator if plan.metadata else "",
            )
        finally:
            if plan.cleanup_archive_after_persist:
                archive_path.unlink(missing_ok=True)


@dataclass(slots=True)
class RuntimeCheckpointCreator:
    runtime: RuntimeCheckpointController
    state_sink: CheckpointStateSink
    persister: WorkerCheckpointPersister
    checkpoint_root: str
    origin_storage_available: bool
    content_cache_available: bool
    id_factory: Callable[[], str] = field(default_factory=lambda: lambda: str(uuid4()))
    nvidia_driver_major: int | None = None
    checkpoint_activity: CheckpointLeaseRegistry = field(default_factory=CheckpointLeaseRegistry)

    def create_checkpoint(
        self,
        instance: WorkerContainerServiceInstance,
        *,
        checkpoint_id: str = "",
    ) -> str:
        capabilities = runtime_capabilities(instance.runtime)
        if not capabilities.checkpoint_restore:
            msg = f"Runtime {instance.runtime.value} does not support checkpoint/restore"
            raise RuntimeError(msg)
        checkpoint_id = checkpoint_id or self.id_factory()
        with self.checkpoint_activity.acquire(checkpoint_id):
            request = build_checkpoint_plan(
                _checkpoint_request(
                    instance,
                    checkpoint_id=checkpoint_id,
                    checkpoint_root=self.checkpoint_root,
                    nvidia_driver_major=self.nvidia_driver_major,
                )
            )
            if not request.available:
                raise RuntimeError(request.reason)
            persistence_plan = plan_checkpoint_persistence(
                CheckpointPersistenceRequest(
                    checkpoint_id=checkpoint_id,
                    checkpoint_root=self.checkpoint_root,
                    origin_storage_available=self.origin_storage_available,
                    content_cache_available=self.content_cache_available,
                    locality=instance.pool_name,
                    gpu=instance.gpu,
                )
            )
            if persistence_plan.error_message:
                raise RuntimeError(persistence_plan.error_message)
            self.state_sink.save_checkpoint_state(
                _pending_checkpoint_payload(instance, checkpoint_id)
            )
            try:
                self.runtime.checkpoint_container(
                    instance.container_id,
                    image_path=request.checkpoint_path,
                    work_dir=request.work_dir,
                    leave_running=request.checkpoint_options.leave_running,
                    allow_open_tcp=request.checkpoint_options.allow_open_tcp,
                    skip_in_flight=request.checkpoint_options.skip_in_flight,
                    link_remap=request.checkpoint_options.link_remap,
                )
                _copy_checkpoint_filesystem(instance, request.checkpoint_path)
                metadata = self.persister.persist_checkpoint(persistence_plan)
                metadata = metadata.model_copy(
                    update={
                        "locality": metadata.locality or instance.pool_name,
                        "accelerator": metadata.accelerator or checkpoint_accelerator(instance.gpu),
                    }
                )
                self.state_sink.save_checkpoint_state(
                    _available_checkpoint_payload(instance, metadata)
                )
            except Exception:
                with suppress(Exception):
                    self.state_sink.save_checkpoint_state(
                        _failed_checkpoint_payload(instance, checkpoint_id)
                    )
                shutil.rmtree(request.checkpoint_path, ignore_errors=True)
                Path(checkpoint_archive_path(self.checkpoint_root, checkpoint_id)).unlink(
                    missing_ok=True
                )
                raise
        return checkpoint_id


@dataclass(slots=True)
class ContainerFilesystemArchiveCreator:
    runtime: RuntimeStateController
    archiver: ContainerImageArchiver
    publisher: ContainerImageArchivePublisher | None = None

    def archive_container(
        self,
        instance: WorkerContainerServiceInstance,
        *,
        image_id: str,
    ) -> Iterable[ContainerArchiveResponse]:
        status = self.runtime.status(instance.container_id)
        if status != "running":
            return (
                ContainerArchiveResponse(
                    done=True,
                    success=False,
                    error_msg="Container not running",
                ),
            )
        responses: list[ContainerArchiveResponse] = []
        try:
            preparation = prepare_container_archive(instance)

            def progress(value: int) -> None:
                responses.append(ContainerArchiveResponse(progress=value))

            result = self.archiver.archive_image(
                Path(preparation.source_path),
                image_id,
                progress,
            )
            if not result.success:
                return (
                    *responses,
                    ContainerArchiveResponse(
                        done=True,
                        success=False,
                        error_msg=result.error_message or "container filesystem archive failed",
                    ),
                )
            if not result.archive_path:
                return (
                    *responses,
                    ContainerArchiveResponse(
                        done=True,
                        success=False,
                        error_msg="container filesystem archive path was not returned",
                    ),
                )
            if self.publisher is None:
                return (
                    *responses,
                    ContainerArchiveResponse(
                        done=True,
                        success=False,
                        error_msg="image archive publisher is not configured",
                    ),
                )
            published = self.publisher.publish_image_archive(
                image_id=image_id,
                archive_path=Path(result.archive_path),
                workspace_id=instance.workspace_id,
                stub_id=instance.stub_id,
            )
            if not published.ok:
                return (
                    *responses,
                    ContainerArchiveResponse(
                        done=True,
                        success=False,
                        error_msg=published.error_message or "image archive publication failed",
                    ),
                )
            responses.append(
                ContainerArchiveResponse(
                    done=True,
                    success=True,
                )
            )
            return tuple(responses)
        except Exception as exc:
            return (ContainerArchiveResponse(done=True, success=False, error_msg=str(exc)),)


@dataclass(slots=True)
class TarContainerImageArchiver:
    target_root: Path
    extension: str = DEFAULT_IMAGE_ARCHIVE_EXTENSION

    def archive_image(
        self,
        source_path: Path,
        image_id: str,
        progress: Callable[[int], None],
    ) -> ContainerImageArchiveResult:
        if not source_path.exists() or not source_path.is_dir():
            return ContainerImageArchiveResult(
                success=False,
                error_message=f"container filesystem source not found: {source_path}",
            )
        target = self.target_root / image_archive_source_key(image_id, extension=self.extension)
        target.parent.mkdir(parents=True, exist_ok=True)
        members = tuple(_iter_archive_members(source_path))
        total = max(len(members), 1)
        last_progress = -1
        try:
            with tarfile.open(target, "w") as archive:
                for index, member in enumerate(members, start=1):
                    archive.add(member, arcname=str(member.relative_to(source_path)))
                    current_progress = min(99, int(index * 100 / total))
                    if current_progress != last_progress:
                        last_progress = current_progress
                        progress(current_progress)
            if last_progress != 100:
                progress(100)
        except OSError as exc:
            target.unlink(missing_ok=True)
            return ContainerImageArchiveResult(
                success=False,
                archive_path=str(target),
                error_message=str(exc),
            )
        return ContainerImageArchiveResult(success=True, archive_path=str(target))


def prepare_container_archive(
    instance: WorkerContainerServiceInstance,
) -> ContainerArchivePreparation:
    source_path = Path(instance.top_layer_path or instance.root_path)
    source_path.mkdir(parents=True, exist_ok=True)
    config = _archive_base_config(instance)
    initial_config = _merge_request_env(config, [*instance.env, *instance.request_env])
    initial_config_path = source_path / ARCHIVE_INITIAL_CONFIG_FILE_NAME
    initial_config_path.write_text(
        json.dumps(initial_config, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    runtime_config = _archive_runtime_config(initial_config)
    runtime_config_path = source_path / OCI_CONFIG_FILE_NAME
    runtime_config_path.write_text(
        json.dumps(runtime_config, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    return ContainerArchivePreparation(
        source_path=str(source_path),
        initial_config_path=str(initial_config_path),
        runtime_config_path=str(runtime_config_path),
        env=_process_env(runtime_config),
    )


def _checkpoint_request(
    instance: WorkerContainerServiceInstance,
    *,
    checkpoint_id: str,
    checkpoint_root: str,
    nvidia_driver_major: int | None,
) -> CheckpointRequest:
    return CheckpointRequest(
        container_id=instance.container_id,
        checkpoint_id=checkpoint_id,
        checkpoint_root=checkpoint_root,
        runtime_name=instance.runtime.value,
        config_path=instance.config_path,
        gpu_count=instance.gpu_count,
        nvidia_driver_major=nvidia_driver_major,
    )


def _available_checkpoint_payload(
    instance: WorkerContainerServiceInstance,
    metadata: WorkerCheckpointPersistenceResult,
) -> CheckpointStatePayload:
    return create_checkpoint_state_payload(
        checkpoint_id=metadata.checkpoint_id,
        source_container_id=instance.container_id,
        status=WorkerCheckpointStatus.Available,
        container_ip=instance.container_ip,
        stub_id=instance.stub_id,
        stub_type=instance.stub_type,
        workspace_id=instance.workspace_id,
        app_id=instance.app_id,
        exposed_ports=instance.exposed_ports,
        cache_hash=metadata.cache_hash,
        cache_size_bytes=metadata.cache_size_bytes,
        origin_key=metadata.origin_key,
        locality=metadata.locality or instance.pool_name,
        accelerator=metadata.accelerator,
    )


def _pending_checkpoint_payload(
    instance: WorkerContainerServiceInstance,
    checkpoint_id: str,
) -> CheckpointStatePayload:
    return create_checkpoint_state_payload(
        checkpoint_id=checkpoint_id,
        source_container_id=instance.container_id,
        status=WorkerCheckpointStatus.Pending,
        container_ip=instance.container_ip,
        stub_id=instance.stub_id,
        stub_type=instance.stub_type,
        workspace_id=instance.workspace_id,
        app_id=instance.app_id,
        exposed_ports=instance.exposed_ports,
    )


def _failed_checkpoint_payload(
    instance: WorkerContainerServiceInstance,
    checkpoint_id: str,
) -> CheckpointStatePayload:
    return create_checkpoint_state_payload(
        checkpoint_id=checkpoint_id,
        source_container_id=instance.container_id,
        status=WorkerCheckpointStatus.CheckpointFailed,
        container_ip=instance.container_ip,
        stub_id=instance.stub_id,
        stub_type=instance.stub_type,
        workspace_id=instance.workspace_id,
        app_id=instance.app_id,
        exposed_ports=instance.exposed_ports,
    )


def _copy_checkpoint_filesystem(
    instance: WorkerContainerServiceInstance,
    checkpoint_path: str,
) -> None:
    source = Path(instance.upper_path or instance.top_layer_path or instance.root_path)
    if not source.exists():
        msg = f"checkpoint filesystem source does not exist: {source}"
        raise RuntimeError(msg)
    destination = Path(checkpoint_path) / CHECKPOINT_FILESYSTEM_DIR
    if destination.exists():
        shutil.rmtree(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(
        source,
        destination,
        symlinks=True,
        ignore=shutil.ignore_patterns(*CHECKPOINT_COPY_EXCLUDES),
        dirs_exist_ok=True,
    )


def _archive_base_config(instance: WorkerContainerServiceInstance) -> JsonObject:
    candidates = [
        Path(instance.config_path) if instance.config_path else None,
        Path(instance.bundle_path) / OCI_CONFIG_FILE_NAME if instance.bundle_path else None,
    ]
    for candidate in candidates:
        if candidate is not None and candidate.exists():
            return _JSON_OBJECT.validate_json(candidate.read_bytes())
    return build_base_oci_config(
        instance.runtime,
        root_path="rootfs",
        env=_env_map(instance.env),
        cwd=instance.cwd,
        hostname=instance.container_id,
        readonly_rootfs=False,
    )


def _archive_runtime_config(config: JsonObject) -> JsonObject:
    prepared = _JSON_OBJECT.validate_python(config)
    hooks = prepared.get("hooks")
    if isinstance(hooks, dict):
        hooks.pop("prestart", None)
    process = _ensure_object(prepared, "process")
    process["terminal"] = False
    process["args"] = ["tail", "-f", "/dev/null"]
    root = _ensure_object(prepared, "root")
    root["readonly"] = False
    return prepared


def _merge_request_env(config: JsonObject, env: list[str]) -> JsonObject:
    prepared = _JSON_OBJECT.validate_python(config)
    process = _ensure_object(prepared, "process")
    merged = _env_map(_process_env(prepared))
    merged.update(_env_map(env))
    process["env"] = [f"{key}={value}" for key, value in sorted(merged.items())]
    return prepared


def _process_env(config: JsonObject) -> list[str]:
    process = config.get("process")
    if not isinstance(process, dict):
        return []
    env = process.get("env")
    if not isinstance(env, list):
        return []
    return [value for value in env if isinstance(value, str)]


def _env_map(env: list[str]) -> dict[str, str]:
    values: dict[str, str] = {}
    for item in env:
        key, separator, value = item.partition("=")
        if separator and key:
            values[key] = value
    return values


def _ensure_object(target: JsonObject, key: str) -> JsonObject:
    value = target.get(key)
    if isinstance(value, dict):
        return value
    replacement: JsonObject = {}
    target[key] = replacement
    return replacement


def _create_tar(source: Path, destination: Path, *, arcname: str) -> None:
    with tarfile.open(destination, "w") as archive:
        archive.add(source, arcname=arcname)


def _file_hash_and_size(path: Path) -> tuple[str, int]:
    hasher = hashlib.sha256()
    size = 0
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            size += len(chunk)
            hasher.update(chunk)
    return hasher.hexdigest(), size


def _iter_archive_members(source_path: Path) -> Iterable[Path]:
    for path in sorted(source_path.rglob("*")):
        if path == source_path:
            continue
        yield path
