from __future__ import annotations

import hashlib
import os
from collections.abc import Callable
from enum import StrEnum
from pathlib import Path
from typing import Protocol

from cache.protocol import (
    CacheContentReadRequest,
    CacheContentReadResult,
    CacheContentReadStatus,
    CacheContentStoreResult,
    CacheContentStoreStatus,
)
from shared.contracts import ContractModel

from worker.image_lifecycle import (
    ImageArchiveStorageMode,
    LocalImageArchiveReadyPlan,
    RestoredImageArchiveValidation,
    plan_local_image_archive_ready,
)
from worker.image_mount import (
    EmbeddedImageArchiveCacheCopyPlan,
    EmbeddedImageArchiveCachePublishPlan,
    ImageArchiveContentCacheRestoreFinishPlan,
    ImageArchiveContentCacheRestorePlan,
    finish_image_archive_content_cache_restore,
    plan_embedded_image_archive_cache_copy,
    plan_embedded_image_archive_cache_publish,
    plan_image_archive_content_cache_restore,
)

ImageArchiveValidator = Callable[
    [Path, ImageArchiveContentCacheRestorePlan],
    RestoredImageArchiveValidation,
]


class WorkerContentCache(Protocol):
    def read_content(self, request: CacheContentReadRequest) -> CacheContentReadResult: ...

    def store_content_from_local_file(
        self,
        path: str | Path,
        *,
        expected_hash: str = "",
        cache_path: str = "",
    ) -> CacheContentStoreResult: ...


class ImageArchiveContentCacheRestoreExecutionStatus(StrEnum):
    Complete = "complete"
    Error = "error"


class ImageArchiveContentCachePublishExecutionStatus(StrEnum):
    Complete = "complete"
    Skipped = "skipped"
    Error = "error"


class WorkerImageArchiveLoadStatus(StrEnum):
    LocalReady = "local-ready"
    RestoredFromContentCache = "restored-from-content-cache"
    SourceFallback = "source-fallback"


class ImageArchiveContentCacheRestoreExecutionResult(ContractModel):
    status: ImageArchiveContentCacheRestoreExecutionStatus
    plan: ImageArchiveContentCacheRestorePlan
    finish: ImageArchiveContentCacheRestoreFinishPlan
    actual_hash: str = ""
    bytes_written: int = 0
    archive_path: str = ""
    temp_path: str = ""
    reason: str = ""

    @property
    def complete(self) -> bool:
        return self.status is ImageArchiveContentCacheRestoreExecutionStatus.Complete


class ImageArchiveContentCachePublishExecutionResult(ContractModel):
    status: ImageArchiveContentCachePublishExecutionStatus
    plan: EmbeddedImageArchiveCachePublishPlan
    content_hash: str = ""
    actual_hash: str = ""
    bytes_stored: int = 0
    reason: str = ""

    @property
    def complete(self) -> bool:
        return self.status is ImageArchiveContentCachePublishExecutionStatus.Complete


class WorkerImageArchiveLocalState(ContractModel):
    exists: bool = False
    is_dir: bool = False
    size_bytes: int = 0
    metadata_valid: bool = True
    storage_mode: ImageArchiveStorageMode = ImageArchiveStorageMode.Local
    has_image_metadata: bool = True
    layer_count: int = 0
    decompressed_hash_count: int = 0


class WorkerImageArchiveLoadResult(ContractModel):
    status: WorkerImageArchiveLoadStatus
    archive_path: str
    image_id: str
    local_ready: LocalImageArchiveReadyPlan | None = None
    copy_plan: EmbeddedImageArchiveCacheCopyPlan | None = None
    restore: ImageArchiveContentCacheRestoreExecutionResult | None = None
    reason: str = ""

    @property
    def should_pull_source(self) -> bool:
        return self.status is WorkerImageArchiveLoadStatus.SourceFallback


def load_image_archive_from_cache_or_source(
    cache: WorkerContentCache,
    *,
    archive_path: str,
    image_id: str,
    cache_path: str,
    validator: ImageArchiveValidator,
    local_state: WorkerImageArchiveLocalState | None = None,
    cache_client_available: bool = True,
    metadata_hash: str = "",
    metadata_size_bytes: int = 0,
    metadata_error: str | None = None,
    cached_reachable: bool = False,
) -> WorkerImageArchiveLoadResult:
    state = local_state or WorkerImageArchiveLocalState()
    local_ready = plan_local_image_archive_ready(
        archive_path=archive_path,
        image_id=image_id,
        exists=state.exists,
        is_dir=state.is_dir,
        size_bytes=state.size_bytes,
        metadata_valid=state.metadata_valid,
        storage_mode=state.storage_mode,
        has_image_metadata=state.has_image_metadata,
        layer_count=state.layer_count,
        decompressed_hash_count=state.decompressed_hash_count,
    )
    if local_ready.ready:
        return WorkerImageArchiveLoadResult(
            status=WorkerImageArchiveLoadStatus.LocalReady,
            archive_path=archive_path,
            image_id=image_id,
            local_ready=local_ready,
            reason=local_ready.reason,
        )

    copy_plan = plan_embedded_image_archive_cache_copy(
        archive_path=archive_path,
        image_id=image_id,
        cache_path=cache_path,
        cache_client_available=cache_client_available,
        metadata_hash=metadata_hash,
        metadata_size_bytes=metadata_size_bytes,
        metadata_error=metadata_error,
        cached_reachable=cached_reachable,
    )
    if not copy_plan.hit:
        return WorkerImageArchiveLoadResult(
            status=WorkerImageArchiveLoadStatus.SourceFallback,
            archive_path=archive_path,
            image_id=image_id,
            local_ready=local_ready,
            copy_plan=copy_plan,
            reason=copy_plan.reason,
        )

    restore_plan = plan_image_archive_content_cache_restore(
        archive_path=archive_path,
        image_id=image_id,
        content_hash=copy_plan.content_hash,
        size_bytes=copy_plan.size_bytes,
        routing_key=copy_plan.routing_key,
    )
    restored = restore_image_archive_from_content_cache(
        cache,
        restore_plan,
        validator=validator,
    )
    if restored.complete:
        return WorkerImageArchiveLoadResult(
            status=WorkerImageArchiveLoadStatus.RestoredFromContentCache,
            archive_path=archive_path,
            image_id=image_id,
            local_ready=local_ready,
            copy_plan=copy_plan,
            restore=restored,
            reason=restored.reason,
        )
    return WorkerImageArchiveLoadResult(
        status=WorkerImageArchiveLoadStatus.SourceFallback,
        archive_path=archive_path,
        image_id=image_id,
        local_ready=local_ready,
        copy_plan=copy_plan,
        restore=restored,
        reason=restored.reason,
    )


def publish_source_image_archive_to_cache(
    cache: WorkerContentCache,
    *,
    archive_path: str,
    image_id: str,
    cache_client_available: bool,
) -> ImageArchiveContentCachePublishExecutionResult:
    return publish_image_archive_to_content_cache(
        cache,
        plan_embedded_image_archive_cache_publish(
            archive_path=archive_path,
            image_id=image_id,
            cache_client_available=cache_client_available,
        ),
    )


def restore_image_archive_from_content_cache(
    cache: WorkerContentCache,
    plan: ImageArchiveContentCacheRestorePlan,
    *,
    validator: ImageArchiveValidator,
) -> ImageArchiveContentCacheRestoreExecutionResult:
    if not plan.ready:
        finish = finish_image_archive_content_cache_restore(plan)
        return _restore_result(
            plan=plan,
            finish=finish,
            status=ImageArchiveContentCacheRestoreExecutionStatus.Error,
            reason=finish.reason,
        )

    archive_path = Path(plan.archive_path)
    temp_path = Path(plan.temp_path)
    temp_path.parent.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256()
    bytes_written = 0
    read_error = ""
    short_read = False

    try:
        with temp_path.open("wb") as output:
            for chunk in plan.chunks:
                read = cache.read_content(
                    CacheContentReadRequest(
                        content_hash=plan.content_hash,
                        offset=chunk.offset,
                        length=chunk.length,
                        routing_key=plan.routing_key,
                    )
                )
                if read.status is not CacheContentReadStatus.Hit:
                    short_read = read.status is CacheContentReadStatus.ShortRead
                    read_error = "" if short_read else read.reason
                    break
                output.write(read.data)
                digest.update(read.data)
                bytes_written += len(read.data)
            if plan.fsync:
                output.flush()
                os.fsync(output.fileno())
    except OSError as exc:
        read_error = str(exc)

    actual_hash = digest.hexdigest() if bytes_written else ""
    validation: RestoredImageArchiveValidation | None = None
    if not read_error and not short_read and actual_hash == plan.content_hash:
        validation = validator(temp_path, plan)
    finish = finish_image_archive_content_cache_restore(
        plan,
        actual_hash=actual_hash,
        read_error=read_error,
        short_read=short_read,
        validation=validation,
    )
    status = (
        ImageArchiveContentCacheRestoreExecutionStatus.Complete
        if finish.complete
        else ImageArchiveContentCacheRestoreExecutionStatus.Error
    )
    if finish.rename_temp_to_archive:
        archive_path.parent.mkdir(parents=True, exist_ok=True)
        temp_path.replace(archive_path)
    if finish.cleanup_temp and temp_path.exists():
        temp_path.unlink()
    return _restore_result(
        plan=plan,
        finish=finish,
        status=status,
        actual_hash=actual_hash,
        bytes_written=bytes_written,
        reason=finish.reason,
    )


def publish_image_archive_to_content_cache(
    cache: WorkerContentCache,
    plan: EmbeddedImageArchiveCachePublishPlan,
) -> ImageArchiveContentCachePublishExecutionResult:
    if not plan.should_publish:
        return ImageArchiveContentCachePublishExecutionResult(
            status=ImageArchiveContentCachePublishExecutionStatus.Skipped,
            plan=plan,
            reason=plan.reason,
        )
    stored = cache.store_content_from_local_file(
        plan.archive_path,
        cache_path=plan.cache_path,
    )
    status = (
        ImageArchiveContentCachePublishExecutionStatus.Complete
        if stored.stored
        else ImageArchiveContentCachePublishExecutionStatus.Error
    )
    return ImageArchiveContentCachePublishExecutionResult(
        status=status,
        plan=plan,
        content_hash=stored.content_hash,
        actual_hash=stored.actual_hash,
        bytes_stored=stored.size_bytes,
        reason=stored.reason
        if stored.status is not CacheContentStoreStatus.SourceMissing
        else "image archive source is missing",
    )


def _restore_result(
    *,
    plan: ImageArchiveContentCacheRestorePlan,
    finish: ImageArchiveContentCacheRestoreFinishPlan,
    status: ImageArchiveContentCacheRestoreExecutionStatus,
    actual_hash: str = "",
    bytes_written: int = 0,
    reason: str = "",
) -> ImageArchiveContentCacheRestoreExecutionResult:
    return ImageArchiveContentCacheRestoreExecutionResult(
        status=status,
        plan=plan,
        finish=finish,
        actual_hash=actual_hash,
        bytes_written=bytes_written,
        archive_path=plan.archive_path,
        temp_path=plan.temp_path,
        reason=reason,
    )
