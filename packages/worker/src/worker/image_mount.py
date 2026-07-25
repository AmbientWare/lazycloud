from __future__ import annotations

import math
from enum import StrEnum

from pydantic import Field, JsonValue
from shared.contracts import ContractModel

from worker.image_lifecycle import (
    DEFAULT_IMAGE_ARCHIVE_EXTENSION,
    ImageArchiveRegistryConfig,
    ImageRegistryStore,
    LazyImageArchivePlan,
    RestoredImageArchiveValidation,
    clip_v1_archive_cache_path,
    clip_v1_archive_data_cache_path,
    clip_v1_archive_data_source_key,
    image_archive_cache_path,
)
from worker.origin_access import CacheOriginCredentials

EMBEDDED_IMAGE_CACHE_LOCK_WAIT_TIMEOUT_SECONDS = 2.0
EMBEDDED_IMAGE_CACHE_WAIT_INTERVAL_SECONDS = 0.25
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


class BrokeredImageArchivePullSource(StrEnum):
    None_ = "none"
    PresignedUrl = "presigned-url"


class V1ArchiveDataCacheSource(StrEnum):
    NotApplicable = "not-applicable"
    LocalReady = "local-ready"
    ContentCacheCopy = "content-cache-copy"
    SourceRegistry = "source-registry"
    BrokeredUrl = "brokered-url"
    Unavailable = "unavailable"


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


class LazyImageS3StorageInfo(ContractModel):
    bucket: str
    region: str = ""
    endpoint_url: str = ""
    key: str
    force_path_style: bool = False
    has_access_key: bool = False
    has_secret_key: bool = False

    @property
    def credentials_available(self) -> bool:
        return self.has_access_key and self.has_secret_key


class LazyImageMountOptionsPlan(ContractModel):
    archive_path: str
    mount_point: str
    cache_path: str = ""
    content_cache_available: bool = False
    use_checkpoints: bool = False
    storage_info: LazyImageS3StorageInfo | None = None
    reason: str = ""


class BrokeredImageArchivePullPlan(ContractModel):
    source: BrokeredImageArchivePullSource = BrokeredImageArchivePullSource.None_
    archive_path: str
    url: str = ""
    reason: str = ""

    @property
    def should_pull(self) -> bool:
        return self.source is not BrokeredImageArchivePullSource.None_


class V1ArchiveDataCachePlan(ContractModel):
    source: V1ArchiveDataCacheSource
    image_id: str
    target_path: str = ""
    cache_path: str = ""
    source_key: str = ""
    routing_key: str = ""
    source_registry: ImageArchiveRegistryConfig | None = None
    brokered_data_url: str = ""
    seed_embedded_cache: bool = False
    reason: str = ""

    @property
    def available(self) -> bool:
        return self.source in {
            V1ArchiveDataCacheSource.LocalReady,
            V1ArchiveDataCacheSource.ContentCacheCopy,
            V1ArchiveDataCacheSource.SourceRegistry,
            V1ArchiveDataCacheSource.BrokeredUrl,
        }


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


class EmbeddedImageArchiveCacheWaitPlan(ContractModel):
    timeout_seconds: float
    interval_seconds: float
    max_attempts: int
    reason: str = ""


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
    temp_path: str = ""
    chunk_size_bytes: int = IMAGE_ARCHIVE_CONTENT_CACHE_RESTORE_CHUNK_BYTES
    chunks: list[ImageArchiveContentCacheReadChunk] = Field(default_factory=list)
    fsync: bool = True
    cleanup_temp: bool = True
    reason: str = ""

    @property
    def ready(self) -> bool:
        return self.status is ImageArchiveContentCacheRestoreStatus.Ready


class ImageArchiveContentCacheRestoreFinishPlan(ContractModel):
    status: ImageArchiveContentCacheRestoreFinishStatus
    archive_path: str
    temp_path: str
    rename_temp_to_archive: bool = False
    cleanup_temp: bool = True
    validation: RestoredImageArchiveValidation | None = None
    reason: str = ""

    @property
    def complete(self) -> bool:
        return self.status is ImageArchiveContentCacheRestoreFinishStatus.Complete


def plan_lazy_image_mount_options(
    *,
    archive: LazyImageArchivePlan,
    mount_point: str,
    content_cache_available: bool = True,
) -> LazyImageMountOptionsPlan:
    cache_available = content_cache_available and archive.content_cache_path != ""
    if archive.uses_oci_storage:
        return LazyImageMountOptionsPlan(
            archive_path=archive.path,
            mount_point=mount_point,
            cache_path=archive.content_cache_path,
            content_cache_available=cache_available,
            use_checkpoints=True,
            reason="OCI archive uses materialized archive content",
        )

    storage_info = lazy_image_s3_storage_info(archive)
    return LazyImageMountOptionsPlan(
        archive_path=archive.path,
        mount_point=mount_point,
        cache_path=archive.content_cache_path,
        content_cache_available=cache_available,
        storage_info=storage_info,
        reason="clip v1 lazy mount uses archive storage info"
        if storage_info is not None
        else "clip v1 lazy mount uses archive-local storage info",
    )


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


def plan_embedded_image_archive_cache_wait(
    *,
    timeout_seconds: float = EMBEDDED_IMAGE_CACHE_LOCK_WAIT_TIMEOUT_SECONDS,
    interval_seconds: float = EMBEDDED_IMAGE_CACHE_WAIT_INTERVAL_SECONDS,
) -> EmbeddedImageArchiveCacheWaitPlan:
    max_attempts = 1
    if timeout_seconds > 0 and interval_seconds > 0:
        max_attempts += math.ceil(timeout_seconds / interval_seconds)
    return EmbeddedImageArchiveCacheWaitPlan(
        timeout_seconds=timeout_seconds,
        interval_seconds=interval_seconds,
        max_attempts=max_attempts,
        reason="wait for contended embedded image archive cache store",
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
    temp_path = f"{archive_path}.tmp" if archive_path else ""
    if not archive_path:
        return ImageArchiveContentCacheRestorePlan(
            status=ImageArchiveContentCacheRestoreStatus.Reject,
            archive_path=archive_path,
            image_id=image_id,
            temp_path=temp_path,
            reason="archive path is required",
        )
    if not image_id:
        return ImageArchiveContentCacheRestorePlan(
            status=ImageArchiveContentCacheRestoreStatus.Reject,
            archive_path=archive_path,
            image_id=image_id,
            temp_path=temp_path,
            reason="image id is required",
        )
    if not content_hash:
        return ImageArchiveContentCacheRestorePlan(
            status=ImageArchiveContentCacheRestoreStatus.Reject,
            archive_path=archive_path,
            image_id=image_id,
            temp_path=temp_path,
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
            temp_path=temp_path,
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
            temp_path=temp_path,
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
        temp_path=temp_path,
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
            temp_path=plan.temp_path,
            reason=plan.reason,
        )
    if read_error:
        return ImageArchiveContentCacheRestoreFinishPlan(
            status=ImageArchiveContentCacheRestoreFinishStatus.Error,
            archive_path=plan.archive_path,
            temp_path=plan.temp_path,
            reason=f"image archive cache read failed: {read_error}",
        )
    if short_read:
        return ImageArchiveContentCacheRestoreFinishPlan(
            status=ImageArchiveContentCacheRestoreFinishStatus.Error,
            archive_path=plan.archive_path,
            temp_path=plan.temp_path,
            reason="short embedded image archive cache read",
        )
    if actual_hash != plan.content_hash:
        return ImageArchiveContentCacheRestoreFinishPlan(
            status=ImageArchiveContentCacheRestoreFinishStatus.Error,
            archive_path=plan.archive_path,
            temp_path=plan.temp_path,
            reason=f"image archive cache hash mismatch: expected {plan.content_hash}",
        )
    if validation is None:
        return ImageArchiveContentCacheRestoreFinishPlan(
            status=ImageArchiveContentCacheRestoreFinishStatus.Error,
            archive_path=plan.archive_path,
            temp_path=plan.temp_path,
            reason="restored image archive validation is required",
        )
    if not validation.valid:
        return ImageArchiveContentCacheRestoreFinishPlan(
            status=ImageArchiveContentCacheRestoreFinishStatus.Error,
            archive_path=plan.archive_path,
            temp_path=plan.temp_path,
            validation=validation,
            reason=validation.reason,
        )
    return ImageArchiveContentCacheRestoreFinishPlan(
        status=ImageArchiveContentCacheRestoreFinishStatus.Complete,
        archive_path=plan.archive_path,
        temp_path=plan.temp_path,
        rename_temp_to_archive=True,
        cleanup_temp=True,
        validation=validation,
        reason="image archive restored from content cache",
    )


def lazy_image_s3_storage_info(
    archive: LazyImageArchivePlan,
) -> LazyImageS3StorageInfo | None:
    registry = archive.source_registry
    if registry is None or not registry.usable:
        return None
    return LazyImageS3StorageInfo(
        bucket=registry.bucket_name,
        region=registry.region,
        endpoint_url=registry.endpoint_url,
        key=clip_v1_archive_data_source_key(archive.image_id),
        force_path_style=registry.force_path_style,
        has_access_key=registry.has_access_key,
        has_secret_key=registry.has_secret_key,
    )


def plan_brokered_image_archive_pull(
    *,
    archive_path: str,
    credentials: CacheOriginCredentials | None,
) -> BrokeredImageArchivePullPlan:
    if credentials is None:
        return BrokeredImageArchivePullPlan(
            archive_path=archive_path,
            reason="origin credentials are unavailable",
        )
    if credentials.image_archive_url:
        return BrokeredImageArchivePullPlan(
            source=BrokeredImageArchivePullSource.PresignedUrl,
            archive_path=archive_path,
            url=credentials.image_archive_url,
            reason="brokered image archive url is available",
        )
    return BrokeredImageArchivePullPlan(
        archive_path=archive_path,
        reason="brokered image archive origin is unavailable",
    )


def mount_option_summary(plan: LazyImageMountOptionsPlan) -> dict[str, JsonValue]:
    return {
        "archive_path": plan.archive_path,
        "mount_point": plan.mount_point,
        "cache_path": plan.cache_path,
        "content_cache_available": plan.content_cache_available,
        "use_checkpoints": plan.use_checkpoints,
        "storage_bucket": plan.storage_info.bucket if plan.storage_info else "",
    }


def plan_v1_archive_data_cache_restore(
    *,
    image_id: str,
    image_registry_store: ImageRegistryStore,
    local_archive_ready: bool = False,
    cache_client_available: bool = True,
    content_cache_copy_available: bool = False,
    source_registry: ImageArchiveRegistryConfig | None = None,
    private_worker: bool = False,
    brokered_data_url: str = "",
    image_cache_path: str = "/cache/images",
    agent_images_path: str = "/images",
) -> V1ArchiveDataCachePlan:
    target_path = clip_v1_archive_data_cache_path(image_id, cache_path=image_cache_path)
    cache_path = clip_v1_archive_cache_path(image_id, agent_images_path=agent_images_path)
    if not image_id or image_registry_store is not ImageRegistryStore.S3:
        return V1ArchiveDataCachePlan(
            source=V1ArchiveDataCacheSource.NotApplicable,
            image_id=image_id,
            reason="v1 archive data cache is only used with s3 image registry storage",
        )
    if local_archive_ready:
        return V1ArchiveDataCachePlan(
            source=V1ArchiveDataCacheSource.LocalReady,
            image_id=image_id,
            target_path=target_path,
            cache_path=cache_path,
            reason="local v1 archive data is ready",
        )
    if not cache_client_available:
        return V1ArchiveDataCachePlan(
            source=V1ArchiveDataCacheSource.Unavailable,
            image_id=image_id,
            target_path=target_path,
            cache_path=cache_path,
            reason="cache client is unavailable",
        )
    if content_cache_copy_available:
        return V1ArchiveDataCachePlan(
            source=V1ArchiveDataCacheSource.ContentCacheCopy,
            image_id=image_id,
            target_path=target_path,
            cache_path=cache_path,
            routing_key=cache_path,
            reason="v1 archive data can be copied from content cache",
        )

    if source_registry is not None and source_registry.usable:
        source_key = clip_v1_archive_data_source_key(image_id)
        return V1ArchiveDataCachePlan(
            source=V1ArchiveDataCacheSource.SourceRegistry,
            image_id=image_id,
            target_path=target_path,
            cache_path=cache_path,
            source_key=source_key,
            routing_key=cache_path,
            source_registry=source_registry,
            reason="v1 archive data can be restored from source registry storage",
        )

    if private_worker and brokered_data_url:
        return V1ArchiveDataCachePlan(
            source=V1ArchiveDataCacheSource.BrokeredUrl,
            image_id=image_id,
            target_path=target_path,
            cache_path=cache_path,
            routing_key=cache_path,
            brokered_data_url=brokered_data_url,
            seed_embedded_cache=True,
            reason="v1 archive data can be restored from brokered url",
        )

    return V1ArchiveDataCachePlan(
        source=V1ArchiveDataCacheSource.Unavailable,
        image_id=image_id,
        target_path=target_path,
        cache_path=cache_path,
        reason="v1 archive data origin is unavailable",
    )
