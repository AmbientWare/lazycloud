from __future__ import annotations

import posixpath
from collections.abc import Mapping
from enum import StrEnum

from shared.contracts import ContractModel

DEFAULT_IMAGE_ARCHIVE_EXTENSION = "rclip"
LOCAL_IMAGE_ARCHIVE_EXTENSION = "clip"
DEFAULT_IMAGE_CACHE_PATH = "/cache/images"
DEFAULT_IMAGE_MOUNT_ROOT = "/mnt/images"
DEFAULT_BUILDAH_ROOT = "/dev/shm"
MAX_EXPECTED_V2_IMAGE_ARCHIVE_SIZE_BYTES = 128 * 1024 * 1024


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
    Unverified = "unverified"
    DigestMismatch = "digest-mismatch"
    Invalid = "invalid"


class ImageRegistryStore(StrEnum):
    S3 = "s3"
    Local = "local"


class BuildahStorageDriver(StrEnum):
    Overlay = "overlay"
    Vfs = "vfs"


class WorkerImagePaths(ContractModel):
    image_id: str
    image_cache_path: str = DEFAULT_IMAGE_CACHE_PATH
    image_mount_root: str = DEFAULT_IMAGE_MOUNT_ROOT
    image_archive_extension: str = DEFAULT_IMAGE_ARCHIVE_EXTENSION

    @property
    def mount_point(self) -> str:
        return image_mount_point(self.image_id, mount_root=self.image_mount_root)

    @property
    def local_archive_path(self) -> str:
        return image_archive_cache_path(
            self.image_id,
            agent_images_path=self.image_cache_path,
            extension=self.image_archive_extension,
        )


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


def image_archive_cache_path(
    image_id: str,
    *,
    agent_images_path: str = "/images",
    extension: str = DEFAULT_IMAGE_ARCHIVE_EXTENSION,
) -> str:
    return posixpath.join(
        agent_images_path.rstrip("/"), image_archive_source_key(image_id, extension=extension)
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
    record. When dispatch names a digest, an archive without that record must pass
    through a verified cache restore or brokered download before its first mount.
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
    if expected_sha256 and not recorded_sha256:
        return LocalImageArchiveReadyPlan(
            status=LocalImageArchiveReadyStatus.Unverified,
            ready=False,
            archive_path=archive_path,
            image_id=image_id,
            reason="local image archive has no verified digest record",
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


def buildah_environment(
    *,
    runroot: str,
    tmpdir: str,
    storage_conf_path: str,
    cpu_count: int,
    base_env: Mapping[str, str] | None = None,
) -> dict[str, str]:
    return {
        **(base_env or {}),
        "TMPDIR": tmpdir,
        "XDG_RUNTIME_DIR": runroot,
        "CONTAINERS_STORAGE_CONF": storage_conf_path,
        "BUILDAH_LAYERS": "true",
        "GOMAXPROCS": "0",
        "PIGZ": f"-p{max(1, cpu_count)}",
    }
