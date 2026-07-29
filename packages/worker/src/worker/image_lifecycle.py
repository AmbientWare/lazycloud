from __future__ import annotations

import json
import posixpath
import re
from datetime import UTC, datetime, timedelta
from enum import StrEnum

from pydantic import Field, JsonValue, field_validator
from shared.contracts import ContractModel

from worker.events import (
    ContainerLifecyclePayload,
    ContainerRequestContext,
    container_lifecycle_from_duration,
)

DEFAULT_IMAGE_ARCHIVE_EXTENSION = "rclip"
LOCAL_IMAGE_ARCHIVE_EXTENSION = "clip"
DEFAULT_IMAGE_CACHE_PATH = "/cache/images"
DEFAULT_IMAGE_MOUNT_ROOT = "/mnt/images"
DEFAULT_IMAGE_BUNDLE_PATH = "/dev/shm/images"
DEFAULT_BUILDAH_ROOT = "/dev/shm"
DEFAULT_IMAGE_PROGRESS_BUCKET_BYTES = 256 * 1024 * 1024
MAX_EXPECTED_V2_IMAGE_ARCHIVE_SIZE_BYTES = 128 * 1024 * 1024
ROLLUP_KEY_SEPARATOR = "\x00"
CLIP_READ_EVENT_QUEUE_SIZE = 65_536
CLIP_READ_PID_RESOLVE_MAX_PARENTS = 64
IMAGE_READ_AGGREGATE_TOP_N = 20


class ImageArchiveStorageMode(StrEnum):
    Unknown = "unknown"
    Local = "local"
    S3 = "s3"
    Oci = "oci"


class RestoredImageArchiveValidationStatus(StrEnum):
    Valid = "valid"
    MetadataInvalid = "metadata-invalid"
    V2ArchiveTooLarge = "v2-archive-too-large"
    V2MissingImageMetadata = "v2-missing-image-metadata"
    V2MissingLayerMetadata = "v2-missing-layer-metadata"


class LocalImageArchiveReadyStatus(StrEnum):
    Ready = "ready"
    Missing = "missing"
    Directory = "directory"
    Empty = "empty"
    DigestMismatch = "digest-mismatch"
    Invalid = "invalid"


class ImageRegistryStore(StrEnum):
    S3 = "s3"
    Local = "local"


class ImageContentKind(StrEnum):
    ClipV1 = "clip-v1"
    ClipV2 = "clip-v2"


class BuildahStorageDriver(StrEnum):
    Overlay = "overlay"
    Vfs = "vfs"


class ImageIndexProgressStage(StrEnum):
    Progress = "progress"
    Completed = "completed"


class ClipReadOperation(StrEnum):
    ClipRead = "clip.read"
    ClipOciRead = "clip.oci_read"


class ClipReadEventQueueAction(StrEnum):
    Ignore = "ignore"
    Enqueue = "enqueue"
    DropFull = "drop-full"


class ClipRuntimePidTrackAction(StrEnum):
    Track = "track"
    Reject = "reject"


class ClipPidResolutionAction(StrEnum):
    ResolveFromPidCache = "resolve-from-pid-cache"
    ResolveFromRuntimePid = "resolve-from-runtime-pid"
    ResolveFromParent = "resolve-from-parent"
    Unresolved = "unresolved"


class ImageCacheEventResult(StrEnum):
    Hit = "hit"
    Miss = "miss"
    Unavailable = "unavailable"
    Error = "error"
    StoredOrPresent = "stored_or_present"


class WorkerImagePaths(ContractModel):
    image_id: str
    image_cache_path: str = DEFAULT_IMAGE_CACHE_PATH
    image_mount_root: str = DEFAULT_IMAGE_MOUNT_ROOT
    image_archive_extension: str = DEFAULT_IMAGE_ARCHIVE_EXTENSION
    local_archive_extension: str = LOCAL_IMAGE_ARCHIVE_EXTENSION

    @property
    def mount_point(self) -> str:
        return image_mount_point(self.image_id, mount_root=self.image_mount_root)

    @property
    def archive_source_key(self) -> str:
        return image_archive_source_key(self.image_id, extension=self.image_archive_extension)

    @property
    def local_archive_path(self) -> str:
        return local_archive_path(
            self.image_id,
            cache_path=self.image_cache_path,
            extension=self.image_archive_extension,
        )

    @property
    def clip_v1_archive_cache_path(self) -> str:
        return clip_v1_archive_cache_path(self.image_id)

    @property
    def clip_v1_archive_data_cache_path(self) -> str:
        return clip_v1_archive_data_cache_path(
            self.image_id,
            cache_path=self.image_cache_path,
            extension=self.local_archive_extension,
        )

    @property
    def clip_v1_archive_data_source_key(self) -> str:
        return clip_v1_archive_data_source_key(
            self.image_id,
            extension=self.local_archive_extension,
        )


class MountedImageHitPlan(ContractModel):
    hit: bool
    phase: str
    duration_ms: int
    lifecycle_id: str
    attrs: dict[str, str] = Field(default_factory=dict)


class ImageArchiveRegistryConfig(ContractModel):
    bucket_name: str = ""
    region: str = ""
    endpoint_url: str = ""
    force_path_style: bool = False
    has_access_key: bool = False
    has_secret_key: bool = False

    @property
    def usable(self) -> bool:
        return self.bucket_name != ""


class LazyImageArchivePlan(ContractModel):
    image_id: str
    path: str
    storage_mode: ImageArchiveStorageMode = ImageArchiveStorageMode.Unknown
    source_registry: ImageArchiveRegistryConfig | None = None
    content_cache_path: str = ""
    use_checkpoints: bool = False

    @property
    def uses_oci_storage(self) -> bool:
        return is_oci_storage_mode(self.storage_mode)


class RestoredImageArchiveValidation(ContractModel):
    status: RestoredImageArchiveValidationStatus
    valid: bool
    image_id: str
    storage_mode: ImageArchiveStorageMode = ImageArchiveStorageMode.Unknown
    size_bytes: int = 0
    cache_oci_metadata: bool = False
    reason: str = ""


class LocalImageArchiveReadyPlan(ContractModel):
    status: LocalImageArchiveReadyStatus
    ready: bool
    archive_path: str
    image_id: str
    remove_path: bool = False
    validation: RestoredImageArchiveValidation | None = None
    reason: str = ""


class OciStorageInfo(ContractModel):
    registry_url: str
    repository: str
    decompressed_hash_by_layer: dict[str, str] = Field(default_factory=dict)


class BuildahDirectoryPlan(ContractModel):
    root: str
    graphroot: str
    runroot: str
    tmpdir: str


class BuildahStorageConfigPlan(ContractModel):
    driver: BuildahStorageDriver
    graphroot: str
    runroot: str
    text: str


class BuildahEnvironmentPlan(ContractModel):
    env: list[str]

    @property
    def env_map(self) -> dict[str, str]:
        result: dict[str, str] = {}
        for item in self.env:
            key, separator, value = item.partition("=")
            if separator:
                result[key] = value
        return result


class ImageIndexProgress(ContractModel):
    stage: ImageIndexProgressStage
    layer_index: int
    total_layers: int
    layer_digest: str = ""
    bytes_processed: int = 0
    bytes_total: int = 0
    compressed_bytes_processed: int = 0
    compressed_bytes_total: int = 0
    source: str = ""


class ImageProgressLogDecision(ContractModel):
    emit: bool
    bucket: int
    message: str = ""


class ClipReadEvent(ContractModel):
    operation: str
    duration_us: int
    bytes_read: int = 0
    path: str = ""
    source: str = ""
    layer_digest: str = ""
    decompressed_hash: str = ""
    success: bool = True
    error: str = ""
    attrs: dict[str, str] = Field(default_factory=dict)
    started_at: datetime | None = None

    @field_validator("duration_us", "bytes_read")
    @classmethod
    def counters_cannot_be_negative(cls, value: int) -> int:
        if value < 0:
            msg = "read event counters cannot be negative"
            raise ValueError(msg)
        return value


class ClipReadEventQueueDecision(ContractModel):
    action: ClipReadEventQueueAction
    accepted: bool
    operation: str
    queue_size: int
    queue_capacity: int
    reason: str = ""


class ClipPidReference(ContractModel):
    container_id: str
    start_time: int

    @field_validator("start_time")
    @classmethod
    def start_time_cannot_be_negative(cls, value: int) -> int:
        if value < 0:
            msg = "process start time cannot be negative"
            raise ValueError(msg)
        return value


class ClipRuntimePidTrackPlan(ContractModel):
    action: ClipRuntimePidTrackAction
    valid: bool
    pid: int
    reference: ClipPidReference | None = None
    reason: str = ""


class ClipProcessInfo(ContractModel):
    pid: int
    start_time: int
    parent_pid: int = 0

    @field_validator("pid", "start_time", "parent_pid")
    @classmethod
    def process_numbers_cannot_be_negative(cls, value: int) -> int:
        if value < 0:
            msg = "process metadata cannot be negative"
            raise ValueError(msg)
        return value


class ClipPidResolutionPlan(ContractModel):
    pid: int
    action: ClipPidResolutionAction
    resolved: bool = False
    container_id: str = ""
    cache_pid: int | None = None
    cache_ref: ClipPidReference | None = None
    stale_cache_pids: list[int] = Field(default_factory=list)
    stale_runtime_pids: list[int] = Field(default_factory=list)
    parent_chain: list[int] = Field(default_factory=list)
    reason: str = ""


class ClipReadRequestResolutionPlan(ContractModel):
    pid: int
    resolved: bool
    container_id: str = ""
    request: ContainerRequestContext | None = None
    pid_resolution: ClipPidResolutionPlan
    reason: str = ""


class ImageCacheAttempt(ContractModel):
    source: str = ""
    result: str = ""
    host_index: int = 0
    host_id: str = ""
    registration_id: str = ""
    pool_name: str = ""
    locality: str = ""
    node_id: str = ""
    cache_path_id: str = ""
    has_endpoint: bool = False
    content_status: str = ""
    elapsed_us: int = 0
    bytes: int = 0
    read: int = 0
    error: str = ""


class ImageContentCacheEvent(ContractModel):
    operation: str
    result: str
    duration_us: int
    bytes: int = 0
    read: int = 0
    error: str = ""
    started_at: datetime | None = None
    attempts: list[ImageCacheAttempt] = Field(default_factory=list)


class ClipReadRollup(ContractModel):
    operation: str = ""
    path: str = ""
    source: str = ""
    layer_digest: str = ""
    decompressed_hash: str = ""
    content_hash: str = ""
    cache_result: str = ""
    cache_tier: str = ""
    count: int = 0
    error_count: int = 0
    total_us: int = 0
    max_us: int = 0
    total_ms: int = 0
    max_ms: int = 0
    bytes_read: int = 0


class ClipCacheRollup(ContractModel):
    operation: str = ""
    result: str = ""
    source: str = ""
    host_index: int = 0
    host_id: str = ""
    registration_id: str = ""
    pool_name: str = ""
    locality: str = ""
    node_id: str = ""
    cache_path_id: str = ""
    has_endpoint: bool = False
    content_status: str = ""
    count: int = 0
    error_count: int = 0
    total_us: int = 0
    max_us: int = 0
    total_ms: int = 0
    max_ms: int = 0
    bytes: int = 0
    read: int = 0


class ClipReadAggregate(ContractModel):
    request: ContainerRequestContext
    success: bool = True
    read_count: int = 0
    error_count: int = 0
    bytes_read: int = 0
    total_us: int = 0
    cache_count: int = 0
    cache_error_count: int = 0
    cache_hit_count: int = 0
    cache_miss_count: int = 0
    cache_unavailable_count: int = 0
    cache_bytes: int = 0
    cache_total_us: int = 0
    started_at: datetime | None = None
    last_at: datetime | None = None
    first_error: str = ""
    sample_attrs: dict[str, str] = Field(default_factory=dict)
    by_access: dict[str, ClipReadRollup] = Field(default_factory=dict)
    by_operation: dict[str, ClipReadRollup] = Field(default_factory=dict)
    by_source: dict[str, ClipReadRollup] = Field(default_factory=dict)
    by_result: dict[str, ClipReadRollup] = Field(default_factory=dict)
    by_layer: dict[str, ClipReadRollup] = Field(default_factory=dict)
    by_content: dict[str, ClipReadRollup] = Field(default_factory=dict)
    by_cache_operation: dict[str, ClipCacheRollup] = Field(default_factory=dict)
    by_cache_result: dict[str, ClipCacheRollup] = Field(default_factory=dict)
    by_cache_source: dict[str, ClipCacheRollup] = Field(default_factory=dict)
    by_cache_host: dict[str, ClipCacheRollup] = Field(default_factory=dict)

    def add_read(self, event: ClipReadEvent) -> None:
        started_at = event.started_at or datetime.now(UTC) - timedelta(
            microseconds=event.duration_us
        )
        ended_at = started_at + timedelta(microseconds=event.duration_us)
        if not event.success:
            self.success = False
            self.error_count += 1
            if not self.first_error:
                self.first_error = event.error
        for key in (
            "cache_result",
            "cache_tier",
            "cached_locally",
            "content_cache_available",
            "content_cache_result",
            "content_cache_warm",
            "fallback",
            "storage_mode",
        ):
            value = event.attrs.get(key, "")
            if value:
                self.sample_attrs[key] = value

        content_hash = event.attrs.get("content_hash", "")
        cache_result = clip_read_cache_result(event)
        self._add_read_rollup(self.by_operation, event.operation, event)
        if cache_result:
            self._add_read_rollup(
                self.by_result,
                clip_read_rollup_key(event.operation, event.source, cache_result),
                event,
            )
        if not is_canonical_clip_read(event.operation):
            return

        self._touch_window(started_at, ended_at)
        self.read_count += 1
        self.bytes_read += event.bytes_read
        self.total_us += event.duration_us
        self._add_read_rollup(
            self.by_access,
            clip_read_rollup_key(
                event.operation,
                event.path,
                event.source,
                event.layer_digest,
                event.decompressed_hash,
                content_hash,
                cache_result,
            ),
            event,
        )
        self._add_read_rollup(self.by_source, event.source, event)
        if event.layer_digest:
            self._add_read_rollup(self.by_layer, event.layer_digest, event)
        content_id = first_non_empty_image_value(event.decompressed_hash, content_hash)
        if content_id:
            self._add_read_rollup(self.by_content, content_id, event)

    def add_content_cache(self, event: ImageContentCacheEvent) -> None:
        started_at = event.started_at or datetime.now(UTC) - timedelta(
            microseconds=event.duration_us
        )
        ended_at = started_at + timedelta(microseconds=event.duration_us)
        self._touch_window(started_at, ended_at)
        self.cache_count += 1
        self.cache_bytes += event.bytes
        self.cache_total_us += event.duration_us
        if image_cache_result_is_hit(event.result):
            self.cache_hit_count += 1
        elif image_cache_result_is_miss(event.result):
            self.cache_miss_count += 1
        elif image_cache_result_is_unavailable(event.result):
            self.cache_unavailable_count += 1
        elif event.result == ImageCacheEventResult.Error:
            self.cache_error_count += 1
        if event.error and event.result != ImageCacheEventResult.Error:
            self.cache_error_count += 1
        if event.error and not self.first_error:
            self.first_error = event.error

        self._add_cache_rollup(
            self.by_cache_operation,
            clip_read_rollup_key(event.operation, event.result),
            operation=event.operation,
            result=event.result,
            source="",
            attempt=None,
            duration_us=event.duration_us,
            bytes=event.bytes,
            read=event.read,
            error=event.error,
        )
        self._add_cache_rollup(
            self.by_cache_result,
            clip_read_rollup_key(event.result, event.operation),
            operation=event.operation,
            result=event.result,
            source="",
            attempt=None,
            duration_us=event.duration_us,
            bytes=event.bytes,
            read=event.read,
            error=event.error,
        )

        attempts = event.attempts or [
            ImageCacheAttempt(
                source="content_cache",
                result=event.result,
                elapsed_us=event.duration_us,
                bytes=event.bytes,
                read=event.read,
                error=event.error,
            )
        ]
        for attempt in attempts:
            source = attempt.source or "unknown"
            result = attempt.result or event.result
            elapsed_us = attempt.elapsed_us or event.duration_us
            self._add_cache_rollup(
                self.by_cache_source,
                clip_read_rollup_key(event.operation, source, result, attempt.content_status),
                operation=event.operation,
                result=result,
                source=source,
                attempt=attempt,
                duration_us=elapsed_us,
                bytes=attempt.bytes,
                read=attempt.read,
                error=attempt.error,
            )
            if attempt.host_id:
                self._add_cache_rollup(
                    self.by_cache_host,
                    clip_read_rollup_key(
                        attempt.host_id,
                        source,
                        result,
                        attempt.content_status,
                    ),
                    operation=event.operation,
                    result=result,
                    source=source,
                    attempt=attempt,
                    duration_us=elapsed_us,
                    bytes=attempt.bytes,
                    read=attempt.read,
                    error=attempt.error,
                )

    def lifecycle_payload(
        self,
        *,
        worker_id: str,
        flush_reason: str,
        lifecycle_id: str = "clip.read",
        top_n: int = IMAGE_READ_AGGREGATE_TOP_N,
    ) -> ContainerLifecyclePayload:
        attrs = self.summary_attrs(flush_reason=flush_reason, top_n=top_n)
        return container_lifecycle_from_duration(
            lifecycle_id,
            self.request,
            started_at=self.started_at or datetime.now(UTC),
            duration=timedelta(microseconds=self.total_us),
            success=self.success,
            attrs=attrs,
            worker_id=worker_id,
        )

    def summary_attrs(
        self, *, flush_reason: str, top_n: int = IMAGE_READ_AGGREGATE_TOP_N
    ) -> dict[str, str]:
        wall_us = 0
        if self.started_at is not None and self.last_at is not None:
            wall_us = max(0, int((self.last_at - self.started_at).total_seconds() * 1_000_000))
        attrs = {
            "aggregate": "true",
            "bytes_read": str(self.bytes_read),
            "cache_bytes": str(self.cache_bytes),
            "cache_duration_us": str(self.cache_total_us),
            "cache_error_count": str(self.cache_error_count),
            "cache_hit_count": str(self.cache_hit_count),
            "cache_miss_count": str(self.cache_miss_count),
            "cache_operation_count": str(self.cache_count),
            "cache_unavailable_count": str(self.cache_unavailable_count),
            "duration_us": str(self.total_us),
            "error_count": str(self.error_count),
            "flush_reason": flush_reason,
            "image_id": self.request.image_id,
            "read_count": str(self.read_count),
            "top_cache_hosts_json": clip_cache_rollups_json(self.by_cache_host, top_n),
            "top_cache_operations_json": clip_cache_rollups_json(self.by_cache_operation, top_n),
            "top_cache_results_json": clip_cache_rollups_json(self.by_cache_result, top_n),
            "top_cache_sources_json": clip_cache_rollups_json(self.by_cache_source, top_n),
            "top_content_json": clip_read_rollups_json(self.by_content, top_n),
            "top_layers_json": clip_read_rollups_json(self.by_layer, top_n),
            "top_operations_json": clip_read_rollups_json(self.by_operation, top_n),
            "top_paths_json": clip_read_rollups_json(self.by_access, top_n),
            "top_results_json": clip_read_rollups_json(self.by_result, top_n),
            "top_sources_json": clip_read_rollups_json(self.by_source, top_n),
            "total_duration_us": str(self.total_us),
            "wall_duration_us": str(wall_us),
        }
        if self.started_at is not None:
            attrs["first_access_at"] = self.started_at.astimezone(UTC).isoformat()
        if self.last_at is not None:
            attrs["last_access_at"] = self.last_at.astimezone(UTC).isoformat()
        if self.first_error:
            attrs["first_error"] = self.first_error
        attrs.update({key: value for key, value in self.sample_attrs.items() if key})
        return attrs

    def _touch_window(self, started_at: datetime, ended_at: datetime) -> None:
        if self.started_at is None or started_at < self.started_at:
            self.started_at = started_at
        if self.last_at is None or ended_at > self.last_at:
            self.last_at = ended_at

    def _add_read_rollup(
        self,
        target: dict[str, ClipReadRollup],
        key: str,
        event: ClipReadEvent,
    ) -> None:
        normalized_key = key or "unknown"
        rollup = target.get(normalized_key)
        if rollup is None:
            rollup = ClipReadRollup(
                operation=event.operation,
                path=event.path,
                source=event.source,
                layer_digest=event.layer_digest,
                decompressed_hash=event.decompressed_hash,
                content_hash=event.attrs.get("content_hash", ""),
                cache_result=clip_read_cache_result(event),
                cache_tier=event.attrs.get("cache_tier", ""),
            )
            target[normalized_key] = rollup
        rollup.count += 1
        rollup.total_us += event.duration_us
        rollup.total_ms = duration_us_to_milliseconds(rollup.total_us)
        if event.duration_us > rollup.max_us:
            rollup.max_us = event.duration_us
            rollup.max_ms = duration_us_to_milliseconds(event.duration_us)
        rollup.bytes_read += event.bytes_read
        if not event.success:
            rollup.error_count += 1

    def _add_cache_rollup(
        self,
        target: dict[str, ClipCacheRollup],
        key: str,
        *,
        operation: str,
        result: str,
        source: str,
        attempt: ImageCacheAttempt | None,
        duration_us: int,
        bytes: int,
        read: int,
        error: str,
    ) -> None:
        normalized_key = key or "unknown"
        rollup = target.get(normalized_key)
        if rollup is None:
            rollup = ClipCacheRollup(operation=operation, result=result, source=source)
            if attempt is not None:
                rollup.host_index = attempt.host_index
                rollup.host_id = attempt.host_id
                rollup.registration_id = attempt.registration_id
                rollup.pool_name = attempt.pool_name
                rollup.locality = attempt.locality
                rollup.node_id = attempt.node_id
                rollup.cache_path_id = attempt.cache_path_id
                rollup.has_endpoint = attempt.has_endpoint
                rollup.content_status = attempt.content_status
            target[normalized_key] = rollup
        rollup.count += 1
        if (
            error
            or image_cache_result_is_miss(result)
            or image_cache_result_is_unavailable(result)
            or result == ImageCacheEventResult.Error
        ):
            rollup.error_count += 1
        rollup.total_us += duration_us
        rollup.total_ms = duration_us_to_milliseconds(rollup.total_us)
        if duration_us > rollup.max_us:
            rollup.max_us = duration_us
            rollup.max_ms = duration_us_to_milliseconds(duration_us)
        rollup.bytes += bytes
        rollup.read += read


def build_worker_image_paths(
    image_id: str,
    *,
    image_cache_path: str = DEFAULT_IMAGE_CACHE_PATH,
    image_mount_root: str = DEFAULT_IMAGE_MOUNT_ROOT,
    image_archive_extension: str = DEFAULT_IMAGE_ARCHIVE_EXTENSION,
) -> WorkerImagePaths:
    return WorkerImagePaths(
        image_id=image_id,
        image_cache_path=image_cache_path.rstrip("/"),
        image_mount_root=image_mount_root.rstrip("/"),
        image_archive_extension=image_archive_extension.lstrip("."),
    )


def image_mount_point(image_id: str, *, mount_root: str = DEFAULT_IMAGE_MOUNT_ROOT) -> str:
    return posixpath.join(mount_root.rstrip("/"), image_id)


def image_archive_source_key(
    image_id: str,
    *,
    extension: str = DEFAULT_IMAGE_ARCHIVE_EXTENSION,
) -> str:
    return f"{image_id}.{extension.lstrip('.')}"


def local_archive_path(
    image_id: str,
    *,
    cache_path: str = DEFAULT_IMAGE_CACHE_PATH,
    extension: str = DEFAULT_IMAGE_ARCHIVE_EXTENSION,
) -> str:
    return posixpath.join(
        cache_path.rstrip("/"), image_archive_source_key(image_id, extension=extension)
    )


def clip_v1_archive_cache_path(
    image_id: str,
    *,
    agent_images_path: str = "/images",
    extension: str = LOCAL_IMAGE_ARCHIVE_EXTENSION,
) -> str:
    return posixpath.join(
        agent_images_path.rstrip("/"), image_archive_source_key(image_id, extension=extension)
    )


def image_archive_cache_path(
    image_id: str,
    *,
    agent_images_path: str = "/images",
    extension: str = DEFAULT_IMAGE_ARCHIVE_EXTENSION,
) -> str:
    return posixpath.join(
        agent_images_path.rstrip("/"), image_archive_source_key(image_id, extension=extension)
    )


def clip_v1_archive_data_cache_path(
    image_id: str,
    *,
    cache_path: str = DEFAULT_IMAGE_CACHE_PATH,
    extension: str = LOCAL_IMAGE_ARCHIVE_EXTENSION,
) -> str:
    return posixpath.join(
        cache_path.rstrip("/"), image_archive_source_key(image_id, extension=extension)
    )


def clip_v1_archive_data_source_key(
    image_id: str,
    *,
    extension: str = LOCAL_IMAGE_ARCHIVE_EXTENSION,
) -> str:
    return image_archive_source_key(image_id, extension=extension)


def mounted_image_hit_plan(
    *,
    mounted_ready: bool,
    phase: str,
    duration_ms: int,
    clip_version: int,
) -> MountedImageHitPlan:
    return MountedImageHitPlan(
        hit=mounted_ready,
        phase=phase,
        duration_ms=duration_ms if mounted_ready else 0,
        lifecycle_id=f"image.{phase}",
        attrs={
            "clip_version": str(clip_version),
            "mounted_fuse_hit": "true" if mounted_ready else "false",
        },
    )


def normalize_archive_storage_mode(
    value: str | ImageArchiveStorageMode | None,
) -> ImageArchiveStorageMode:
    if isinstance(value, ImageArchiveStorageMode):
        return value
    normalized = (value or "").strip().lower()
    for mode in ImageArchiveStorageMode:
        if normalized == mode.value:
            return mode
    return ImageArchiveStorageMode.Unknown


def is_oci_storage_mode(value: str | ImageArchiveStorageMode | None) -> bool:
    return normalize_archive_storage_mode(value) is ImageArchiveStorageMode.Oci


def validate_restored_image_archive(
    *,
    image_id: str,
    size_bytes: int,
    metadata_valid: bool,
    storage_mode: str | ImageArchiveStorageMode,
    has_image_metadata: bool = False,
    layer_count: int = 0,
    decompressed_hash_count: int = 0,
    max_v2_size_bytes: int = MAX_EXPECTED_V2_IMAGE_ARCHIVE_SIZE_BYTES,
) -> RestoredImageArchiveValidation:
    normalized_mode = normalize_archive_storage_mode(storage_mode)
    if not metadata_valid:
        return RestoredImageArchiveValidation(
            status=RestoredImageArchiveValidationStatus.MetadataInvalid,
            valid=False,
            image_id=image_id,
            storage_mode=normalized_mode,
            size_bytes=size_bytes,
            reason="restored image archive metadata is invalid",
        )
    if normalized_mode is not ImageArchiveStorageMode.Oci:
        return RestoredImageArchiveValidation(
            status=RestoredImageArchiveValidationStatus.Valid,
            valid=True,
            image_id=image_id,
            storage_mode=normalized_mode,
            size_bytes=size_bytes,
            reason="non-oci image archive metadata is valid",
        )
    if size_bytes > max_v2_size_bytes:
        return RestoredImageArchiveValidation(
            status=RestoredImageArchiveValidationStatus.V2ArchiveTooLarge,
            valid=False,
            image_id=image_id,
            storage_mode=normalized_mode,
            size_bytes=size_bytes,
            reason="restored v2 image archive is unexpectedly large",
        )
    if not has_image_metadata:
        return RestoredImageArchiveValidation(
            status=RestoredImageArchiveValidationStatus.V2MissingImageMetadata,
            valid=False,
            image_id=image_id,
            storage_mode=normalized_mode,
            size_bytes=size_bytes,
            reason="restored v2 image archive is missing embedded image metadata",
        )
    if layer_count <= 0 or decompressed_hash_count <= 0:
        return RestoredImageArchiveValidation(
            status=RestoredImageArchiveValidationStatus.V2MissingLayerMetadata,
            valid=False,
            image_id=image_id,
            storage_mode=normalized_mode,
            size_bytes=size_bytes,
            reason="restored v2 image archive has no layer cache metadata",
        )
    return RestoredImageArchiveValidation(
        status=RestoredImageArchiveValidationStatus.Valid,
        valid=True,
        image_id=image_id,
        storage_mode=normalized_mode,
        size_bytes=size_bytes,
        cache_oci_metadata=True,
        reason="restored v2 image archive metadata is valid",
    )


def plan_local_image_archive_ready(
    *,
    archive_path: str,
    image_id: str,
    exists: bool,
    is_dir: bool = False,
    size_bytes: int = 0,
    expected_sha256: str = "",
    recorded_sha256: str = "",
    metadata_valid: bool = True,
    storage_mode: str | ImageArchiveStorageMode = ImageArchiveStorageMode.Local,
    has_image_metadata: bool = False,
    layer_count: int = 0,
    decompressed_hash_count: int = 0,
) -> LocalImageArchiveReadyPlan:
    """Whether the archive already on this worker can be reused for this dispatch.

    `expected_sha256` is the digest the dispatch is authorized for and
    `recorded_sha256` is what the worker recorded when it materialized these bytes.
    Comparing the two records is what makes a stale or foreign local copy visible;
    rehashing the archive would cost its full size on every container start, and the
    download path already verified the bytes against the digest that produced the
    record. Either side being empty means nothing is being claimed, so the archive is
    judged exactly as it was before.
    """
    if not exists:
        return LocalImageArchiveReadyPlan(
            status=LocalImageArchiveReadyStatus.Missing,
            ready=False,
            archive_path=archive_path,
            image_id=image_id,
            reason="local image archive is missing",
        )
    if is_dir:
        return LocalImageArchiveReadyPlan(
            status=LocalImageArchiveReadyStatus.Directory,
            ready=False,
            archive_path=archive_path,
            image_id=image_id,
            remove_path=True,
            reason="local image archive path is a directory",
        )
    if size_bytes <= 0:
        return LocalImageArchiveReadyPlan(
            status=LocalImageArchiveReadyStatus.Empty,
            ready=False,
            archive_path=archive_path,
            image_id=image_id,
            remove_path=True,
            reason="local image archive is empty",
        )
    if expected_sha256 and recorded_sha256 and expected_sha256 != recorded_sha256:
        return LocalImageArchiveReadyPlan(
            status=LocalImageArchiveReadyStatus.DigestMismatch,
            ready=False,
            archive_path=archive_path,
            image_id=image_id,
            remove_path=True,
            reason="local image archive holds different bytes than this request authorizes",
        )

    validation = validate_restored_image_archive(
        image_id=image_id,
        size_bytes=size_bytes,
        metadata_valid=metadata_valid,
        storage_mode=storage_mode,
        has_image_metadata=has_image_metadata,
        layer_count=layer_count,
        decompressed_hash_count=decompressed_hash_count,
    )
    if not validation.valid:
        return LocalImageArchiveReadyPlan(
            status=LocalImageArchiveReadyStatus.Invalid,
            ready=False,
            archive_path=archive_path,
            image_id=image_id,
            remove_path=True,
            validation=validation,
            reason=validation.reason,
        )
    return LocalImageArchiveReadyPlan(
        status=LocalImageArchiveReadyStatus.Ready,
        ready=True,
        archive_path=archive_path,
        image_id=image_id,
        validation=validation,
        reason="local image archive is ready",
    )


def plan_content_cache_path(
    *,
    image_id: str,
    container_id: str,
    storage_mode: ImageArchiveStorageMode,
    registry_store: ImageRegistryStore,
    local_cache_enabled: bool,
    image_cache_path: str = DEFAULT_IMAGE_CACHE_PATH,
    build_container_prefix: str = "build-",
) -> str:
    if is_oci_storage_mode(storage_mode):
        return image_cache_path.rstrip("/")
    if registry_store is ImageRegistryStore.S3:
        return clip_v1_archive_data_cache_path(image_id, cache_path=image_cache_path)
    if local_cache_enabled or container_id.startswith(build_container_prefix):
        return posixpath.join(image_cache_path.rstrip("/"), f"{image_id}.cache")
    return ""


def plan_lazy_image_archive(
    *,
    image_id: str,
    container_id: str,
    archive_path: str,
    storage_mode: str | ImageArchiveStorageMode,
    registry_store: ImageRegistryStore,
    local_cache_enabled: bool,
    source_registry: ImageArchiveRegistryConfig | None = None,
) -> LazyImageArchivePlan:
    normalized_mode = normalize_archive_storage_mode(storage_mode)
    return LazyImageArchivePlan(
        image_id=image_id,
        path=archive_path,
        storage_mode=normalized_mode,
        source_registry=source_registry if source_registry and source_registry.usable else None,
        content_cache_path=plan_content_cache_path(
            image_id=image_id,
            container_id=container_id,
            storage_mode=normalized_mode,
            registry_store=registry_store,
            local_cache_enabled=local_cache_enabled,
        ),
        use_checkpoints=is_oci_storage_mode(normalized_mode),
    )


def select_build_registry(
    *,
    configured_registry: str = "",
    runner_base_image_registry: str = "",
) -> str:
    return configured_registry or runner_base_image_registry or "localhost"


def plan_buildah_directories(root: str = DEFAULT_BUILDAH_ROOT) -> BuildahDirectoryPlan:
    base = root.rstrip("/")
    return BuildahDirectoryPlan(
        root=base,
        graphroot=posixpath.join(base, "buildah-storage"),
        runroot=posixpath.join(base, "buildah-run"),
        tmpdir=posixpath.join(base, "buildah-tmp"),
    )


def plan_buildah_storage_config(
    directories: BuildahDirectoryPlan,
    *,
    driver: BuildahStorageDriver = BuildahStorageDriver.Overlay,
) -> BuildahStorageConfigPlan:
    lines = [
        "[storage]",
        f'driver = "{driver.value}"',
        f'graphroot = "{directories.graphroot}"',
        f'runroot = "{directories.runroot}"',
    ]
    if driver is BuildahStorageDriver.Overlay:
        lines.extend(
            [
                "",
                "[storage.options.overlay]",
                'mountopt = "nodev"',
                'force_mask = "0000"',
            ]
        )
    return BuildahStorageConfigPlan(
        driver=driver,
        graphroot=directories.graphroot,
        runroot=directories.runroot,
        text="\n".join(lines) + "\n",
    )


def plan_buildah_environment(
    *,
    runroot: str,
    tmpdir: str,
    storage_conf_path: str,
    cpu_count: int,
    base_env: list[str] | None = None,
) -> BuildahEnvironmentPlan:
    env = list(base_env or [])
    env.extend(
        [
            f"TMPDIR={tmpdir}",
            f"XDG_RUNTIME_DIR={runroot}",
            f"CONTAINERS_STORAGE_CONF={storage_conf_path}",
            "BUILDAH_LAYERS=true",
            "GOMAXPROCS=0",
            f"PIGZ=-p{max(1, cpu_count)}",
        ]
    )
    return BuildahEnvironmentPlan(env=env)


def local_oci_layout_ref(digest: str) -> str:
    if not digest:
        return "latest"
    ref = re.sub(r"[:/+@]+", "-", digest)
    ref = re.sub(r"[^A-Za-z0-9._-]+", "-", ref).strip("-")
    return (ref or "latest")[:128]


def format_image_bytes(value: int) -> str:
    if value >= 1 << 30:
        return f"{value / (1 << 30):.2f} GiB"
    if value >= 1 << 20:
        return f"{value / (1 << 20):.1f} MiB"
    return f"{value // 1024} KiB"


def image_index_progress_key(progress: ImageIndexProgress) -> str:
    return progress.layer_digest or str(progress.layer_index)


def should_log_image_index_progress(
    progress: ImageIndexProgress,
    buckets: dict[str, int],
    *,
    bucket_bytes: int = DEFAULT_IMAGE_PROGRESS_BUCKET_BYTES,
) -> ImageProgressLogDecision:
    key = image_index_progress_key(progress)
    if progress.compressed_bytes_total > 0 and progress.compressed_bytes_processed > 0:
        bucket = min(
            10, progress.compressed_bytes_processed * 10 // progress.compressed_bytes_total
        )
        if bucket <= buckets.get(key, 0):
            return ImageProgressLogDecision(emit=False, bucket=bucket)
        buckets[key] = bucket
        return ImageProgressLogDecision(
            emit=True,
            bucket=bucket,
            message=format_image_index_progress(progress),
        )
    bucket = progress.bytes_processed // bucket_bytes
    if bucket == 0 or bucket <= buckets.get(key, 0):
        return ImageProgressLogDecision(emit=False, bucket=bucket)
    buckets[key] = bucket
    return ImageProgressLogDecision(
        emit=True,
        bucket=bucket,
        message=format_image_index_progress(progress),
    )


def format_image_index_progress(progress: ImageIndexProgress) -> str:
    prefix = f"Indexing layer {progress.layer_index}/{progress.total_layers}"
    if progress.compressed_bytes_total > 0 and progress.compressed_bytes_processed > 0:
        percent = min(
            100, progress.compressed_bytes_processed * 100 // progress.compressed_bytes_total
        )
        return (
            f"{prefix}... {percent}% "
            f"({format_image_bytes(progress.compressed_bytes_processed)}/"
            f"{format_image_bytes(progress.compressed_bytes_total)} read, "
            f"{format_image_bytes(progress.bytes_processed)} indexed)"
        )
    if progress.bytes_processed > 0:
        return f"{prefix}... {format_image_bytes(progress.bytes_processed)} indexed"
    return f"{prefix}..."


def format_image_index_completed(progress: ImageIndexProgress) -> str:
    indexed_bytes = progress.bytes_total or progress.bytes_processed
    if progress.compressed_bytes_total > 0 and indexed_bytes > 0:
        return (
            f"Indexed layer {progress.layer_index}/{progress.total_layers} "
            f"(100%, {format_image_bytes(progress.compressed_bytes_total)} read, "
            f"{format_image_bytes(indexed_bytes)} indexed)"
        )
    if indexed_bytes > 0:
        return (
            f"Indexed layer {progress.layer_index}/{progress.total_layers} "
            f"({format_image_bytes(indexed_bytes)} indexed)"
        )
    return f"Indexed layer {progress.layer_index}/{progress.total_layers}"


def plan_clip_read_event_enqueue(
    *,
    operation: str,
    queue_size: int,
    queue_capacity: int = CLIP_READ_EVENT_QUEUE_SIZE,
) -> ClipReadEventQueueDecision:
    if not operation.startswith("clip."):
        return ClipReadEventQueueDecision(
            action=ClipReadEventQueueAction.Ignore,
            accepted=False,
            operation=operation,
            queue_size=queue_size,
            queue_capacity=queue_capacity,
            reason="event is not a clip read event",
        )
    if queue_capacity <= 0 or queue_size >= queue_capacity:
        return ClipReadEventQueueDecision(
            action=ClipReadEventQueueAction.DropFull,
            accepted=False,
            operation=operation,
            queue_size=queue_size,
            queue_capacity=queue_capacity,
            reason="clip read event queue is full",
        )
    return ClipReadEventQueueDecision(
        action=ClipReadEventQueueAction.Enqueue,
        accepted=True,
        operation=operation,
        queue_size=queue_size,
        queue_capacity=queue_capacity,
        reason="event can be enqueued",
    )


def plan_clip_runtime_pid_tracking(
    *,
    container_id: str,
    pid: int,
    start_time: int,
) -> ClipRuntimePidTrackPlan:
    if not container_id:
        return ClipRuntimePidTrackPlan(
            action=ClipRuntimePidTrackAction.Reject,
            valid=False,
            pid=pid,
            reason="container id is required",
        )
    if pid <= 0:
        return ClipRuntimePidTrackPlan(
            action=ClipRuntimePidTrackAction.Reject,
            valid=False,
            pid=pid,
            reason="pid must be positive",
        )
    if start_time <= 0:
        return ClipRuntimePidTrackPlan(
            action=ClipRuntimePidTrackAction.Reject,
            valid=False,
            pid=pid,
            reason="process start time is required",
        )
    return ClipRuntimePidTrackPlan(
        action=ClipRuntimePidTrackAction.Track,
        valid=True,
        pid=pid,
        reference=ClipPidReference(container_id=container_id, start_time=start_time),
        reason="runtime pid can be tracked",
    )


def process_start_times_equal(current_start_time: int, cached_start_time: int) -> bool:
    return (
        current_start_time > 0 and cached_start_time > 0 and current_start_time == cached_start_time
    )


def resolve_clip_read_container_id(
    pid: int,
    *,
    pid_cache: dict[int, ClipPidReference],
    runtime_pids: dict[int, ClipPidReference],
    process_table: dict[int, ClipProcessInfo],
    max_parents: int = CLIP_READ_PID_RESOLVE_MAX_PARENTS,
) -> ClipPidResolutionPlan:
    if pid <= 0:
        return ClipPidResolutionPlan(
            pid=pid,
            action=ClipPidResolutionAction.Unresolved,
            reason="pid must be positive",
        )

    stale_cache_pids: list[int] = []
    stale_runtime_pids: list[int] = []
    process = process_table.get(pid)

    cached_ref = pid_cache.get(pid)
    if cached_ref is not None:
        if process is not None and process_start_times_equal(
            process.start_time, cached_ref.start_time
        ):
            return ClipPidResolutionPlan(
                pid=pid,
                action=ClipPidResolutionAction.ResolveFromPidCache,
                resolved=True,
                container_id=cached_ref.container_id,
                reason="pid cache entry matches process start time",
            )
        stale_cache_pids.append(pid)

    runtime_ref = runtime_pids.get(pid)
    if runtime_ref is not None:
        if process is not None and process_start_times_equal(
            process.start_time, runtime_ref.start_time
        ):
            return ClipPidResolutionPlan(
                pid=pid,
                action=ClipPidResolutionAction.ResolveFromRuntimePid,
                resolved=True,
                container_id=runtime_ref.container_id,
                reason="runtime pid entry matches process start time",
                stale_cache_pids=stale_cache_pids,
            )
        stale_runtime_pids.append(pid)

    if process is None:
        return ClipPidResolutionPlan(
            pid=pid,
            action=ClipPidResolutionAction.Unresolved,
            stale_cache_pids=stale_cache_pids,
            stale_runtime_pids=stale_runtime_pids,
            reason="process metadata is unavailable",
        )

    original_start_time = process.start_time
    current_pid = pid
    parent_chain: list[int] = []
    for _ in range(max(0, max_parents)):
        if current_pid <= 1:
            break
        current = process_table.get(current_pid)
        if current is None or current.start_time == 0:
            return ClipPidResolutionPlan(
                pid=pid,
                action=ClipPidResolutionAction.Unresolved,
                stale_cache_pids=stale_cache_pids,
                stale_runtime_pids=stale_runtime_pids,
                parent_chain=parent_chain,
                reason="parent process metadata is unavailable",
            )
        parent_chain.append(current_pid)

        parent_ref = runtime_pids.get(current_pid)
        if parent_ref is not None:
            if not process_start_times_equal(current.start_time, parent_ref.start_time):
                stale_runtime_pids.append(current_pid)
                if current.parent_pid <= 0 or current.parent_pid == current_pid:
                    break
                current_pid = current.parent_pid
                continue
            cache_ref = ClipPidReference(
                container_id=parent_ref.container_id,
                start_time=original_start_time,
            )
            return ClipPidResolutionPlan(
                pid=pid,
                action=ClipPidResolutionAction.ResolveFromParent,
                resolved=True,
                container_id=parent_ref.container_id,
                cache_pid=pid,
                cache_ref=cache_ref,
                stale_cache_pids=stale_cache_pids,
                stale_runtime_pids=stale_runtime_pids,
                parent_chain=parent_chain,
                reason="resolved through runtime parent pid",
            )

        if current.parent_pid <= 0 or current.parent_pid == current_pid:
            break
        current_pid = current.parent_pid

    return ClipPidResolutionPlan(
        pid=pid,
        action=ClipPidResolutionAction.Unresolved,
        stale_cache_pids=stale_cache_pids,
        stale_runtime_pids=stale_runtime_pids,
        parent_chain=parent_chain,
        reason="no tracked runtime pid found in parent chain",
    )


def resolve_clip_read_request(
    pid: int,
    *,
    active_containers: dict[str, ContainerRequestContext],
    pid_cache: dict[int, ClipPidReference],
    runtime_pids: dict[int, ClipPidReference],
    process_table: dict[int, ClipProcessInfo],
    max_parents: int = CLIP_READ_PID_RESOLVE_MAX_PARENTS,
) -> ClipReadRequestResolutionPlan:
    pid_resolution = resolve_clip_read_container_id(
        pid,
        pid_cache=pid_cache,
        runtime_pids=runtime_pids,
        process_table=process_table,
        max_parents=max_parents,
    )
    if not pid_resolution.resolved:
        return ClipReadRequestResolutionPlan(
            pid=pid,
            resolved=False,
            pid_resolution=pid_resolution,
            reason=pid_resolution.reason,
        )
    request = active_containers.get(pid_resolution.container_id)
    if request is None:
        return ClipReadRequestResolutionPlan(
            pid=pid,
            resolved=False,
            container_id=pid_resolution.container_id,
            pid_resolution=pid_resolution,
            reason="resolved container is not active",
        )
    return ClipReadRequestResolutionPlan(
        pid=pid,
        resolved=True,
        container_id=pid_resolution.container_id,
        request=request,
        pid_resolution=pid_resolution,
        reason="resolved active container request",
    )


def is_canonical_clip_read(operation: str) -> bool:
    return operation in {ClipReadOperation.ClipRead, ClipReadOperation.ClipOciRead}


def clip_read_cache_result(event: ClipReadEvent) -> str:
    return first_non_empty_image_value(
        event.attrs.get("cache_result", ""),
        event.attrs.get("content_cache_result", ""),
    )


def clip_read_rollup_key(*parts: str) -> str:
    return ROLLUP_KEY_SEPARATOR.join(parts) + ROLLUP_KEY_SEPARATOR


def duration_us_to_milliseconds(duration_us: int) -> int:
    if duration_us <= 0:
        return 0
    return (duration_us + 999) // 1000


def first_non_empty_image_value(*values: str) -> str:
    for value in values:
        if value:
            return value
    return ""


def image_cache_result_is_hit(result: str | ImageCacheEventResult) -> bool:
    return result in {
        ImageCacheEventResult.Hit,
        ImageCacheEventResult.StoredOrPresent,
        "already_present",
        "already_present_after_lock",
        "lock_wait_present",
    }


def image_cache_result_is_miss(result: str | ImageCacheEventResult) -> bool:
    result_value = result.value if isinstance(result, ImageCacheEventResult) else result
    return result_value in {
        "miss",
        "missing",
        "partial",
        "size_mismatch",
        "stored",
    } or result_value.startswith("stored_")


def image_cache_result_is_unavailable(result: str | ImageCacheEventResult) -> bool:
    return result in {ImageCacheEventResult.Unavailable, "lock_unavailable"}


def clip_read_rollups_json(rollups: dict[str, ClipReadRollup], limit: int) -> str:
    if not rollups or limit <= 0:
        return "[]"
    items = sorted(
        rollups.values(),
        key=lambda item: (item.total_us, item.max_us),
        reverse=True,
    )[:limit]
    return json.dumps(
        [item.model_dump(mode="json", exclude_defaults=True) for item in items],
        separators=(",", ":"),
    )


def clip_cache_rollups_json(rollups: dict[str, ClipCacheRollup], limit: int) -> str:
    if not rollups or limit <= 0:
        return "[]"
    items = sorted(
        rollups.values(),
        key=lambda item: (item.total_us, item.max_us),
        reverse=True,
    )[:limit]
    return json.dumps(
        [item.model_dump(mode="json", exclude_defaults=True) for item in items],
        separators=(",", ":"),
    )


def redact_image_plan(value: JsonValue) -> JsonValue:
    if isinstance(value, dict):
        return {
            key: "<redacted>"
            if "secret" in key.lower() or "password" in key.lower()
            else redact_image_plan(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact_image_plan(item) for item in value]
    return value
