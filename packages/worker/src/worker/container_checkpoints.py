from __future__ import annotations

import shutil
from collections.abc import Callable, Iterable
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol
from uuid import uuid4

from shared.checkpoints import CheckpointRecord
from shared.container_requests import WORKER_USER_ARTIFACT_VOLUME
from shared.contracts import ContractModel
from shared.image_building.authoring import FilesystemSnapshotMetadata

from worker.checkpoint_activity import CheckpointLeaseRegistry
from worker.checkpoint_filesystem import copy_checkpoint_filesystem
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
from worker.container_service.protocols import WorkerSandboxControlManagerFactory
from worker.execution import (
    CHECKPOINT_FILESYSTEM_DIR,
    checkpoint_accelerator,
    checkpoint_archive_path,
    image_environment,
)
from worker.runtime_config import runtime_capabilities


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


class WorkerCheckpointPersister(Protocol):
    def persist_checkpoint(
        self,
        plan: CheckpointPersistencePlan,
    ) -> WorkerCheckpointPersistenceResult: ...


class WorkerCheckpointPersistenceResult(ContractModel):
    checkpoint_id: str
    archive_path: str
    origin_key: str
    cache_hash: str
    cache_size_bytes: int
    locality: str = ""
    accelerator: str = ""


@dataclass(slots=True)
class RuntimeCheckpointCreator:
    runtime: RuntimeCheckpointController
    state_sink: CheckpointStateSink
    persister: WorkerCheckpointPersister
    checkpoint_root: str
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
                    content_cache_available=self.content_cache_available,
                    locality=instance.pool,
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
                        "locality": metadata.locality or instance.pool,
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
    process_managers: WorkerSandboxControlManagerFactory

    def archive_container(
        self,
        instance: WorkerContainerServiceInstance,
    ) -> Iterable[ContainerArchiveResponse]:
        snapshot = instance.filesystem_snapshot
        if snapshot is None:
            yield ContainerArchiveResponse(
                done=True,
                error_msg="Container filesystem snapshot ownership metadata is unavailable",
            )
            return
        if self.runtime.status(instance.container_id) != "running":
            yield ContainerArchiveResponse(done=True, error_msg="Container not running")
            return
        manager = self.process_managers.create_process_manager(instance)
        stream = manager.snapshot_filesystem(exclude_paths=snapshot.excluded_paths)
        try:
            for data in stream:
                yield ContainerArchiveResponse(data=data)
            yield ContainerArchiveResponse(
                done=True,
                success=True,
                metadata=FilesystemSnapshotMetadata(
                    env=image_environment(snapshot.image_config.env),
                    workdir=instance.cwd,
                    architecture=snapshot.architecture,
                ),
            )
        except Exception as exc:
            yield ContainerArchiveResponse(done=True, error_msg=str(exc))
        finally:
            stream.close()


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
        locality=metadata.locality or instance.pool,
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
    copy_checkpoint_filesystem(
        source,
        destination,
        excluded_root_entries=(WORKER_USER_ARTIFACT_VOLUME.strip("/"),),
    )
