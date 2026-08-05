from __future__ import annotations

import shutil
import tarfile
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from cache.protocol import CacheContentReadRequest, CacheContentReadStatus
from foundation.process import ProcessOutputSink
from pydantic import BaseModel, ConfigDict
from shared.checkpoints import CheckpointRecord
from shared.container_requests import WorkerStartupKind

from worker.checkpoint_activity import CheckpointLeaseRegistry
from worker.checkpoints import (
    CheckpointArchiveMaterializationRequest,
    CheckpointLifecycleAction,
    CheckpointRequest,
    CheckpointRestoreRequest,
    CheckpointStatePayload,
    WorkerCheckpointStatus,
    build_restore_plan,
    checkpoint_archive_hash_and_size,
    mark_checkpoint_restored_payload,
    plan_checkpoint_archive_materialization,
    plan_checkpoint_restore,
    plan_checkpoint_restore_result,
    update_checkpoint_status_payload,
    validate_checkpoint_archive,
)
from worker.container_execution import ContainerExecutionContext, ContainerRuntimeRunResult
from worker.execution import CHECKPOINT_FILESYSTEM_DIR
from worker.image_archive_cache import WorkerContentCache
from worker.oci_spec import OciRuntimeContainerSpec
from worker.runtime_config import runtime_capabilities

CHECKPOINT_CACHE_READ_CHUNK_BYTES = 8 * 1024 * 1024


class _OciRootConfig(BaseModel):
    model_config = ConfigDict(extra="allow")

    path: str


class _OciConfig(BaseModel):
    model_config = ConfigDict(extra="allow")

    root: _OciRootConfig


class CheckpointRestoreSource(Protocol):
    def get_checkpoint(self, checkpoint_id: str, *, workspace_id: str) -> CheckpointRecord: ...

    def download_checkpoint(self, checkpoint: CheckpointRecord, target: Path) -> None: ...


class CheckpointRestoreStateSink(Protocol):
    def save_checkpoint_state(self, payload: CheckpointStatePayload) -> CheckpointRecord: ...


class CheckpointRuntimeController(Protocol):
    def restore_container(
        self,
        container_id: str,
        *,
        image_path: str,
        work_dir: str,
        bundle_path: str,
        on_started: Callable[[int], None],
        output_sink: ProcessOutputSink | None = None,
        tcp_close: bool = True,
        link_remap: bool = True,
    ) -> ContainerRuntimeRunResult: ...


@dataclass(frozen=True, slots=True)
class _PreparedRestoreFilesystem:
    config_path: Path
    original_config: str
    staged_rootfs: Path

    def reset(self) -> None:
        try:
            self.config_path.write_text(self.original_config, encoding="utf-8")
        finally:
            shutil.rmtree(self.staged_rootfs, ignore_errors=True)


def assemble_checkpoint_archive_from_cache(
    cache: WorkerContentCache | None,
    checkpoint: CheckpointRecord,
    archive_path: Path,
) -> bool:
    """Rebuild a checkpoint archive from the content cache, or report a miss.

    A miss is ordinary: the caller falls through to origin storage and pays for
    the download. A short read is reported as a miss rather than as a shorter
    archive, so a partial cache entry can never be restored as the checkpoint.
    """
    if cache is None or not checkpoint.cache_hash or checkpoint.cache_size_bytes <= 0:
        return False
    remaining = checkpoint.cache_size_bytes
    offset = 0
    try:
        with archive_path.open("wb") as handle:
            while remaining > 0:
                length = min(CHECKPOINT_CACHE_READ_CHUNK_BYTES, remaining)
                result = cache.read_content(
                    CacheContentReadRequest(
                        content_hash=checkpoint.cache_hash,
                        offset=offset,
                        length=length,
                        routing_key=checkpoint.cache_hash,
                    )
                )
                if result.status is not CacheContentReadStatus.Hit or not result.data:
                    return False
                handle.write(result.data)
                offset += len(result.data)
                remaining -= len(result.data)
    except OSError:
        return False
    return remaining == 0


@dataclass(slots=True)
class RuntimeCheckpointRestorer:
    source: CheckpointRestoreSource
    state_sink: CheckpointRestoreStateSink
    runtime: CheckpointRuntimeController
    checkpoint_root: str
    checkpoint_activity: CheckpointLeaseRegistry = field(default_factory=CheckpointLeaseRegistry)
    cache: WorkerContentCache | None = None

    def restore(
        self,
        context: ContainerExecutionContext,
        spec: OciRuntimeContainerSpec,
        *,
        on_started: Callable[[int], None],
        output_sink: ProcessOutputSink | None = None,
    ) -> ContainerRuntimeRunResult | None:
        if not context.checkpoint_id:
            return None
        checkpoint_lease = self.checkpoint_activity.acquire(context.checkpoint_id)
        checkpoint: CheckpointRecord | None = None
        prepared_filesystem: _PreparedRestoreFilesystem | None = None
        restore_started = False
        try:
            checkpoint = self.source.get_checkpoint(
                context.checkpoint_id,
                workspace_id=context.request.workspace_id,
            )
            materialized = self._materialized(checkpoint.checkpoint_id)
            decision = plan_checkpoint_restore(
                CheckpointRestoreRequest(
                    checkpoint_id=checkpoint.checkpoint_id,
                    checkpoint_status=WorkerCheckpointStatus(checkpoint.status.value),
                    supports_checkpoint=runtime_capabilities(context.runtime).checkpoint_restore,
                    materialized=materialized,
                    has_complete_metadata=bool(
                        checkpoint.cache_hash
                        and checkpoint.cache_size_bytes > 0
                        and checkpoint.origin_key
                    ),
                    stub_is_deployment=context.startup_kind is not WorkerStartupKind.Sandbox,
                )
            )
            if decision.action is CheckpointLifecycleAction.Materialize:
                self._ensure_materialized(checkpoint)
            elif decision.action is not CheckpointLifecycleAction.Restore:
                raise RuntimeError(decision.reason)

            prepared_filesystem = self._restore_filesystem(
                checkpoint.checkpoint_id,
                spec,
            )
            restore = build_restore_plan(
                CheckpointRequest(
                    container_id=context.request.container_id,
                    checkpoint_id=checkpoint.checkpoint_id,
                    checkpoint_root=self.checkpoint_root,
                    runtime_name=context.runtime.value,
                    config_path=spec.config_path,
                    gpu_count=context.request.gpu_count,
                )
            )
            if not restore.available:
                raise RuntimeError(restore.reason)

            def checkpoint_started(pid: int) -> None:
                nonlocal restore_started
                self.state_sink.save_checkpoint_state(
                    mark_checkpoint_restored_payload(checkpoint.checkpoint_id)
                )
                restore_started = True
                on_started(pid)
                checkpoint_lease.close()

            result = self.runtime.restore_container(
                context.request.container_id,
                image_path=restore.image_path,
                work_dir=restore.work_dir,
                bundle_path=restore.bundle_path,
                on_started=checkpoint_started,
                output_sink=output_sink,
                tcp_close=restore.restore_options.tcp_close,
            )
            return result
        except Exception as exc:
            if not restore_started and prepared_filesystem is not None:
                prepared_filesystem.reset()
            if checkpoint is not None and not restore_started:
                self.state_sink.save_checkpoint_state(
                    update_checkpoint_status_payload(
                        checkpoint.checkpoint_id,
                        WorkerCheckpointStatus.RestoreFailed,
                    )
                )
            if not restore_started:
                decision = plan_checkpoint_restore_result(
                    restored=False,
                    stub_is_deployment=context.startup_kind is not WorkerStartupKind.Sandbox,
                    error_message=str(exc),
                )
                if decision.action is CheckpointLifecycleAction.FallbackRun:
                    return None
            raise
        finally:
            checkpoint_lease.close()

    def _materialized(self, checkpoint_id: str) -> bool:
        checkpoint_path = Path(self.checkpoint_root) / checkpoint_id
        if not (checkpoint_path / CHECKPOINT_FILESYSTEM_DIR).is_dir():
            return False
        return any(entry.name != CHECKPOINT_FILESYSTEM_DIR for entry in checkpoint_path.iterdir())

    def _ensure_materialized(self, checkpoint: CheckpointRecord) -> None:
        with self.checkpoint_activity.acquire_materialization(checkpoint.checkpoint_id):
            if self._materialized(checkpoint.checkpoint_id):
                return
            self._materialize(checkpoint)
            if not self._materialized(checkpoint.checkpoint_id):
                raise RuntimeError("checkpoint archive is missing its runtime payload")

    def _materialize(self, checkpoint: CheckpointRecord) -> None:
        plan = plan_checkpoint_archive_materialization(
            CheckpointArchiveMaterializationRequest(
                checkpoint_id=checkpoint.checkpoint_id,
                checkpoint_root=self.checkpoint_root,
                cache_hash=checkpoint.cache_hash,
                cache_size_bytes=checkpoint.cache_size_bytes,
                origin_key=checkpoint.origin_key,
                materialized=False,
                cache_available=self.cache is not None,
                origin_storage_available=True,
                locality=checkpoint.locality,
                accelerator=checkpoint.accelerator,
            )
        )
        if plan.error_message:
            raise RuntimeError(plan.error_message)
        archive_path = Path(plan.archive_path)
        archive_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            # The plan orders the cache ahead of origin storage. A cache miss is
            # not a failure: fall through and pay for the download instead.
            from_cache = assemble_checkpoint_archive_from_cache(
                self.cache, checkpoint, archive_path
            )
            if not from_cache:
                self.source.download_checkpoint(checkpoint, archive_path)
            actual_hash, actual_size = checkpoint_archive_hash_and_size(archive_path)
            validation = validate_checkpoint_archive(
                expected_hash=checkpoint.cache_hash,
                expected_size_bytes=checkpoint.cache_size_bytes,
                actual_hash=actual_hash,
                actual_size_bytes=actual_size,
            )
            if not validation.ok:
                raise RuntimeError(validation.reason)
            if not from_cache and plan.store_download_in_cache:
                self._store_in_cache(checkpoint, archive_path)
            _extract_checkpoint_archive(
                archive_path,
                checkpoint_path=Path(plan.checkpoint_path),
                temporary_root=Path(plan.temporary_extract_root),
                checkpoint_id=checkpoint.checkpoint_id,
            )
        finally:
            archive_path.unlink(missing_ok=True)

    def _store_in_cache(self, checkpoint: CheckpointRecord, archive_path: Path) -> None:
        cache = self.cache
        if cache is None or not checkpoint.cache_hash:
            return
        # Best effort: a cache that refuses the archive costs the next worker a
        # download, it does not make this restore wrong.
        cache.store_content_from_local_file(
            archive_path,
            expected_hash=checkpoint.cache_hash,
            cache_path=checkpoint.origin_key,
        )

    def _restore_filesystem(
        self,
        checkpoint_id: str,
        container_spec: OciRuntimeContainerSpec,
    ) -> _PreparedRestoreFilesystem:
        source = Path(self.checkpoint_root) / checkpoint_id / CHECKPOINT_FILESYSTEM_DIR
        if not source.is_dir():
            raise RuntimeError("checkpoint archive is missing its filesystem payload")
        config_path = Path(container_spec.config_path)
        original_config = config_path.read_text(encoding="utf-8")
        config = _OciConfig.model_validate_json(original_config)
        staged_rootfs = Path(container_spec.bundle_path) / "checkpoint-rootfs"
        shutil.rmtree(staged_rootfs, ignore_errors=True)
        try:
            shutil.copytree(source, staged_rootfs, symlinks=True)
            config.root.path = str(staged_rootfs)
            config_path.write_text(
                config.model_dump_json(indent=2),
                encoding="utf-8",
            )
        except Exception:
            config_path.write_text(original_config, encoding="utf-8")
            shutil.rmtree(staged_rootfs, ignore_errors=True)
            raise
        return _PreparedRestoreFilesystem(
            config_path=config_path,
            original_config=original_config,
            staged_rootfs=staged_rootfs,
        )


def _extract_checkpoint_archive(
    archive_path: Path,
    *,
    checkpoint_path: Path,
    temporary_root: Path,
    checkpoint_id: str,
) -> None:
    shutil.rmtree(temporary_root, ignore_errors=True)
    temporary_root.mkdir(parents=True, exist_ok=True)
    try:
        with tarfile.open(archive_path) as archive:
            archive.extractall(temporary_root, filter="data")
        extracted = temporary_root / checkpoint_id
        if not (extracted / CHECKPOINT_FILESYSTEM_DIR).is_dir():
            raise RuntimeError("checkpoint archive is missing its filesystem payload")
        shutil.rmtree(checkpoint_path, ignore_errors=True)
        checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
        extracted.replace(checkpoint_path)
    finally:
        shutil.rmtree(temporary_root, ignore_errors=True)
