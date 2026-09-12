from __future__ import annotations

from enum import StrEnum

from pydantic import Field
from shared.contracts import ContractModel

from worker.image_lifecycle import (
    DEFAULT_IMAGE_ARCHIVE_EXTENSION,
    RestoredImageArchiveValidation,
    image_archive_cache_path,
)

MAX_EMBEDDED_ARCHIVE_METADATA_SIZE_BYTES = (1 << 63) - 1
IMAGE_ARCHIVE_CONTENT_CACHE_RESTORE_CHUNK_BYTES = 4 * 1024 * 1024


class ImageContentCacheResult(StrEnum):
    Miss = "miss"
    Unavailable = "unavailable"
    Error = "error"


class ImageContentCacheErrorClassification(ContractModel):
    result: ImageContentCacheResult | None = None
    reason: str


def classify_image_content_cache_error(
    error: str | None,
) -> ImageContentCacheErrorClassification:
    normalized = (error or "").strip().lower().replace("-", "_").replace(" ", "_")
    if not normalized:
        return ImageContentCacheErrorClassification(reason="no cache error")
    compact = normalized.replace("_", "")
    if "content_not_found" in normalized or "contentnotfound" in compact:
        return ImageContentCacheErrorClassification(
            result=ImageContentCacheResult.Miss,
            reason="content cache miss",
        )
    unavailable_markers = (
        "unavailable",
        "unable_to_reach",
        "connection_refused",
        "timed_out",
        "timeout",
        "host_not_found",
        "client_not_found",
    )
    if any(marker in normalized for marker in unavailable_markers):
        return ImageContentCacheErrorClassification(
            result=ImageContentCacheResult.Unavailable,
            reason="content cache unavailable",
        )
    return ImageContentCacheErrorClassification(
        result=ImageContentCacheResult.Error,
        reason="content cache error",
    )


class EmbeddedImageArchiveCacheCopyStatus(StrEnum):
    Hit = "hit"
    Miss = "miss"
    Error = "error"


class ImageArchiveContentCacheRestoreStatus(StrEnum):
    Ready = "ready"
    Reject = "reject"


class ImageArchiveContentCacheRestoreFinishStatus(StrEnum):
    Complete = "complete"
    Error = "error"


class EmbeddedImageArchiveCachePublishPlan(ContractModel):
    should_publish: bool
    archive_path: str
    image_id: str
    cache_path: str = ""
    routing_key: str = ""
    lock: bool = True
    reason: str = ""


class EmbeddedImageArchiveCacheCopyPlan(ContractModel):
    status: EmbeddedImageArchiveCacheCopyStatus
    archive_path: str
    image_id: str
    cache_path: str
    content_hash: str = ""
    size_bytes: int = 0
    routing_key: str = ""
    reason: str = ""

    @property
    def hit(self) -> bool:
        return self.status is EmbeddedImageArchiveCacheCopyStatus.Hit


class ImageArchiveContentCacheReadChunk(ContractModel):
    offset: int
    length: int


class ImageArchiveContentCacheRestorePlan(ContractModel):
    status: ImageArchiveContentCacheRestoreStatus
    archive_path: str
    image_id: str
    content_hash: str = ""
    size_bytes: int = 0
    routing_key: str = ""
    chunk_size_bytes: int = IMAGE_ARCHIVE_CONTENT_CACHE_RESTORE_CHUNK_BYTES
    chunks: list[ImageArchiveContentCacheReadChunk] = Field(default_factory=list)
    fsync: bool = True
    reason: str = ""

    @property
    def ready(self) -> bool:
        return self.status is ImageArchiveContentCacheRestoreStatus.Ready


class ImageArchiveContentCacheRestoreFinishPlan(ContractModel):
    status: ImageArchiveContentCacheRestoreFinishStatus
    archive_path: str
    rename_temp_to_archive: bool = False
    validation: RestoredImageArchiveValidation | None = None
    reason: str = ""

    @property
    def complete(self) -> bool:
        return self.status is ImageArchiveContentCacheRestoreFinishStatus.Complete


def plan_embedded_image_archive_cache_publish(
    *,
    archive_path: str,
    image_id: str,
    cache_client_available: bool,
    agent_images_path: str = "/images",
    extension: str = DEFAULT_IMAGE_ARCHIVE_EXTENSION,
) -> EmbeddedImageArchiveCachePublishPlan:
    if not cache_client_available:
        return EmbeddedImageArchiveCachePublishPlan(
            should_publish=False,
            archive_path=archive_path,
            image_id=image_id,
            reason="cache client unavailable",
        )
    if not archive_path:
        return EmbeddedImageArchiveCachePublishPlan(
            should_publish=False,
            archive_path=archive_path,
            image_id=image_id,
            reason="archive path is required",
        )
    if not image_id:
        return EmbeddedImageArchiveCachePublishPlan(
            should_publish=False,
            archive_path=archive_path,
            image_id=image_id,
            reason="image id is required",
        )
    cache_path = image_archive_cache_path(
        image_id,
        agent_images_path=agent_images_path,
        extension=extension,
    )
    return EmbeddedImageArchiveCachePublishPlan(
        should_publish=True,
        archive_path=archive_path,
        image_id=image_id,
        cache_path=cache_path,
        routing_key=cache_path,
        reason="publish image archive to embedded content cache",
    )


def plan_embedded_image_archive_cache_copy(
    *,
    archive_path: str,
    image_id: str,
    cache_path: str,
    cache_client_available: bool,
    expected_sha256: str = "",
    metadata_hash: str = "",
    metadata_size_bytes: int = 0,
    metadata_error: str | None = None,
    cached_reachable: bool = False,
    write_error: str | None = None,
    max_metadata_size_bytes: int = MAX_EMBEDDED_ARCHIVE_METADATA_SIZE_BYTES,
) -> EmbeddedImageArchiveCacheCopyPlan:
    if not cache_client_available:
        return EmbeddedImageArchiveCacheCopyPlan(
            status=EmbeddedImageArchiveCacheCopyStatus.Miss,
            archive_path=archive_path,
            image_id=image_id,
            cache_path=cache_path,
            reason="cache client unavailable",
        )

    metadata_error_class = classify_image_content_cache_error(metadata_error)
    if metadata_error_class.result is ImageContentCacheResult.Miss:
        return EmbeddedImageArchiveCacheCopyPlan(
            status=EmbeddedImageArchiveCacheCopyStatus.Miss,
            archive_path=archive_path,
            image_id=image_id,
            cache_path=cache_path,
            reason="embedded cache metadata miss",
        )
    if metadata_error_class.result is not None:
        return EmbeddedImageArchiveCacheCopyPlan(
            status=EmbeddedImageArchiveCacheCopyStatus.Error,
            archive_path=archive_path,
            image_id=image_id,
            cache_path=cache_path,
            reason=metadata_error_class.reason,
        )
    if not metadata_hash:
        return EmbeddedImageArchiveCacheCopyPlan(
            status=EmbeddedImageArchiveCacheCopyStatus.Error,
            archive_path=archive_path,
            image_id=image_id,
            cache_path=cache_path,
            reason="cache metadata is missing content hash",
        )
    if expected_sha256 and metadata_hash != expected_sha256:
        # The content cache is keyed on the image id alone and shared by every
        # workspace on this worker, so without this a local archive refused for
        # holding the wrong bytes would simply be restored from the same wrong bytes
        # again. Treat it as a miss and let the broker re-resolve the archive.
        return EmbeddedImageArchiveCacheCopyPlan(
            status=EmbeddedImageArchiveCacheCopyStatus.Miss,
            archive_path=archive_path,
            image_id=image_id,
            cache_path=cache_path,
            content_hash=metadata_hash,
            size_bytes=metadata_size_bytes,
            reason="cached image archive holds different bytes than this request authorizes",
        )
    if metadata_size_bytes > max_metadata_size_bytes:
        return EmbeddedImageArchiveCacheCopyPlan(
            status=EmbeddedImageArchiveCacheCopyStatus.Error,
            archive_path=archive_path,
            image_id=image_id,
            cache_path=cache_path,
            content_hash=metadata_hash,
            size_bytes=metadata_size_bytes,
            reason="image archive is too large for local restore",
        )
    if not cached_reachable:
        return EmbeddedImageArchiveCacheCopyPlan(
            status=EmbeddedImageArchiveCacheCopyStatus.Miss,
            archive_path=archive_path,
            image_id=image_id,
            cache_path=cache_path,
            content_hash=metadata_hash,
            size_bytes=metadata_size_bytes,
            reason="cached image archive content is not reachable",
        )

    write_error_class = classify_image_content_cache_error(write_error)
    if write_error_class.result is ImageContentCacheResult.Miss:
        return EmbeddedImageArchiveCacheCopyPlan(
            status=EmbeddedImageArchiveCacheCopyStatus.Miss,
            archive_path=archive_path,
            image_id=image_id,
            cache_path=cache_path,
            content_hash=metadata_hash,
            size_bytes=metadata_size_bytes,
            reason="embedded cache archive write missed content",
        )
    if write_error_class.result is not None:
        return EmbeddedImageArchiveCacheCopyPlan(
            status=EmbeddedImageArchiveCacheCopyStatus.Error,
            archive_path=archive_path,
            image_id=image_id,
            cache_path=cache_path,
            content_hash=metadata_hash,
            size_bytes=metadata_size_bytes,
            reason=write_error_class.reason,
        )

    return EmbeddedImageArchiveCacheCopyPlan(
        status=EmbeddedImageArchiveCacheCopyStatus.Hit,
        archive_path=archive_path,
        image_id=image_id,
        cache_path=cache_path,
        content_hash=metadata_hash,
        size_bytes=metadata_size_bytes,
        routing_key=cache_path,
        reason="image archive can be restored from embedded content cache",
    )


def plan_image_archive_content_cache_restore(
    *,
    archive_path: str,
    image_id: str,
    content_hash: str,
    size_bytes: int,
    routing_key: str = "",
    chunk_size_bytes: int = IMAGE_ARCHIVE_CONTENT_CACHE_RESTORE_CHUNK_BYTES,
) -> ImageArchiveContentCacheRestorePlan:
    if not archive_path:
        return ImageArchiveContentCacheRestorePlan(
            status=ImageArchiveContentCacheRestoreStatus.Reject,
            archive_path=archive_path,
            image_id=image_id,
            reason="archive path is required",
        )
    if not image_id:
        return ImageArchiveContentCacheRestorePlan(
            status=ImageArchiveContentCacheRestoreStatus.Reject,
            archive_path=archive_path,
            image_id=image_id,
            reason="image id is required",
        )
    if not content_hash:
        return ImageArchiveContentCacheRestorePlan(
            status=ImageArchiveContentCacheRestoreStatus.Reject,
            archive_path=archive_path,
            image_id=image_id,
            reason="content hash is required",
        )
    if size_bytes <= 0:
        return ImageArchiveContentCacheRestorePlan(
            status=ImageArchiveContentCacheRestoreStatus.Reject,
            archive_path=archive_path,
            image_id=image_id,
            content_hash=content_hash,
            size_bytes=size_bytes,
            routing_key=routing_key or content_hash,
            reason="image archive size must be positive",
        )
    if chunk_size_bytes <= 0:
        return ImageArchiveContentCacheRestorePlan(
            status=ImageArchiveContentCacheRestoreStatus.Reject,
            archive_path=archive_path,
            image_id=image_id,
            content_hash=content_hash,
            size_bytes=size_bytes,
            routing_key=routing_key or content_hash,
            reason="content cache restore chunk size must be positive",
        )

    chunks = [
        ImageArchiveContentCacheReadChunk(
            offset=offset,
            length=min(chunk_size_bytes, size_bytes - offset),
        )
        for offset in range(0, size_bytes, chunk_size_bytes)
    ]
    return ImageArchiveContentCacheRestorePlan(
        status=ImageArchiveContentCacheRestoreStatus.Ready,
        archive_path=archive_path,
        image_id=image_id,
        content_hash=content_hash,
        size_bytes=size_bytes,
        routing_key=routing_key or content_hash,
        chunk_size_bytes=chunk_size_bytes,
        chunks=chunks,
        reason="image archive can be restored from content cache chunks",
    )


def finish_image_archive_content_cache_restore(
    plan: ImageArchiveContentCacheRestorePlan,
    *,
    actual_hash: str = "",
    read_error: str = "",
    short_read: bool = False,
    validation: RestoredImageArchiveValidation | None = None,
) -> ImageArchiveContentCacheRestoreFinishPlan:
    if not plan.ready:
        return ImageArchiveContentCacheRestoreFinishPlan(
            status=ImageArchiveContentCacheRestoreFinishStatus.Error,
            archive_path=plan.archive_path,
            reason=plan.reason,
        )
    if read_error:
        return ImageArchiveContentCacheRestoreFinishPlan(
            status=ImageArchiveContentCacheRestoreFinishStatus.Error,
            archive_path=plan.archive_path,
            reason=f"image archive cache read failed: {read_error}",
        )
    if short_read:
        return ImageArchiveContentCacheRestoreFinishPlan(
            status=ImageArchiveContentCacheRestoreFinishStatus.Error,
            archive_path=plan.archive_path,
            reason="short embedded image archive cache read",
        )
    if actual_hash != plan.content_hash:
        return ImageArchiveContentCacheRestoreFinishPlan(
            status=ImageArchiveContentCacheRestoreFinishStatus.Error,
            archive_path=plan.archive_path,
            reason=f"image archive cache hash mismatch: expected {plan.content_hash}",
        )
    if validation is None:
        return ImageArchiveContentCacheRestoreFinishPlan(
            status=ImageArchiveContentCacheRestoreFinishStatus.Error,
            archive_path=plan.archive_path,
            reason="restored image archive validation is required",
        )
    if not validation.valid:
        return ImageArchiveContentCacheRestoreFinishPlan(
            status=ImageArchiveContentCacheRestoreFinishStatus.Error,
            archive_path=plan.archive_path,
            validation=validation,
            reason=validation.reason,
        )
    return ImageArchiveContentCacheRestoreFinishPlan(
        status=ImageArchiveContentCacheRestoreFinishStatus.Complete,
        archive_path=plan.archive_path,
        rename_temp_to_archive=True,
        validation=validation,
        reason="image archive restored from content cache",
    )
