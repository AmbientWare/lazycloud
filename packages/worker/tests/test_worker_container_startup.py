from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path

from cache.protocol import (
    CacheContentReadRequest,
    CacheContentReadResult,
    CacheContentStoreResult,
)
from worker.container_startup import (
    IMAGE_MOUNT_MANIFEST_NAME,
    ImageMountManifest,
    WorkerImageArchiveCacheMetadata,
    WorkerImageMountRequest,
    WorkerImageMountResult,
    WorkerImageMountStatus,
    WorkerImageSourceLoadRequest,
    WorkerImageSourceLoadResult,
    WorkerImageStartupLoader,
)
from worker.events import ContainerRequestContext
from worker.image_lifecycle import ImageRuntimeConfig


@dataclass(slots=True)
class _UnreachableContentCache:
    def read_content(self, request: CacheContentReadRequest) -> CacheContentReadResult:
        raise AssertionError(f"content cache must not be read: {request.content_hash}")

    def store_content_from_local_file(
        self,
        path: str | Path,
        *,
        expected_hash: str = "",
        cache_path: str = "",
    ) -> CacheContentStoreResult:
        raise AssertionError(f"content cache must not be written: {path}")


@dataclass(slots=True)
class _MissingCacheMetadata:
    def image_archive_metadata(self, cache_path: str) -> WorkerImageArchiveCacheMetadata:
        return WorkerImageArchiveCacheMetadata(error=f"content_not_found: {cache_path}")


@dataclass(slots=True)
class _RecordingMounter:
    requests: list[WorkerImageMountRequest] = field(default_factory=list)

    def mount_image_archive(self, request: WorkerImageMountRequest) -> WorkerImageMountResult:
        self.requests.append(request)
        return WorkerImageMountResult(
            status=WorkerImageMountStatus.Ready,
            mount_point=request.mount_point,
            image_config=ImageRuntimeConfig(),
            reason="image archive materialized",
        )


@dataclass(slots=True)
class _BrokerSourceLoader:
    payload: bytes
    archive_sha256: str
    requests: list[WorkerImageSourceLoadRequest] = field(default_factory=list)

    def load_source_image_archive(
        self,
        request: WorkerImageSourceLoadRequest,
    ) -> WorkerImageSourceLoadResult:
        self.requests.append(request)
        Path(request.archive_path).write_bytes(self.payload)
        return WorkerImageSourceLoadResult(
            ok=True,
            archive_path=request.archive_path,
            archive_sha256=self.archive_sha256,
            bytes_written=len(self.payload),
            reason="brokered image archive downloaded and verified",
        )


def test_unrecorded_local_archive_is_verified_before_first_mount(tmp_path: Path) -> None:
    cache_root = tmp_path / "cache" / "images"
    mount_root = tmp_path / "mnt" / "images"
    unrecorded = b"archive-without-a-completed-mount"
    authorized = b"archive-this-request-is-authorized-for"
    authorized_sha256 = hashlib.sha256(authorized).hexdigest()

    archive_path = cache_root / "image-1.rclip"
    archive_path.parent.mkdir(parents=True)
    archive_path.write_bytes(unrecorded)

    mounter = _RecordingMounter()
    source_loader = _BrokerSourceLoader(payload=authorized, archive_sha256=authorized_sha256)
    loader = WorkerImageStartupLoader(
        mounter=mounter,
        cache=_UnreachableContentCache(),
        source_loader=source_loader,
        cache_metadata=_MissingCacheMetadata(),
        image_cache_path=str(cache_root),
        image_mount_root=str(mount_root),
        publish_source_to_cache=False,
    )

    result = loader.load_image(
        ContainerRequestContext(
            container_id="ctr-1",
            image_id="image-1",
            archive_sha256=authorized_sha256,
        )
    )

    assert result.loaded
    assert [request.archive_path for request in source_loader.requests] == [str(archive_path)]
    assert archive_path.read_bytes() == authorized
    assert [request.archive_sha256 for request in mounter.requests] == [authorized_sha256]


def test_cached_image_is_refused_when_it_records_other_archive_bytes(tmp_path: Path) -> None:
    """A local image cached under other archive bytes is re-fetched, not mounted.

    The image id alone is what the worker's cache has always keyed on, and it is
    shared by every workspace the worker runs. Only the dispatched digest was
    resolved against this request's own authorization, so a cached mount recording
    different bytes has to send the worker back to the broker.
    """
    cache_root = tmp_path / "cache" / "images"
    mount_root = tmp_path / "mnt" / "images"
    cached = b"archive-materialized-for-another-request"
    cached_sha256 = hashlib.sha256(cached).hexdigest()
    authorized = b"archive-this-request-is-authorized-for"
    authorized_sha256 = hashlib.sha256(authorized).hexdigest()

    archive_path = cache_root / "image-1.rclip"
    archive_path.parent.mkdir(parents=True)
    archive_path.write_bytes(cached)
    mount_point = mount_root / "image-1"
    mount_point.mkdir(parents=True)
    (mount_point / IMAGE_MOUNT_MANIFEST_NAME).write_text(
        ImageMountManifest(
            image_id="image-1",
            archive_sha256=cached_sha256,
            archive_size_bytes=len(cached),
            archive_entry_count=1,
        ).model_dump_json(),
        encoding="utf-8",
    )

    mounter = _RecordingMounter()
    source_loader = _BrokerSourceLoader(payload=authorized, archive_sha256=authorized_sha256)
    loader = WorkerImageStartupLoader(
        mounter=mounter,
        cache=_UnreachableContentCache(),
        source_loader=source_loader,
        cache_metadata=_MissingCacheMetadata(),
        image_cache_path=str(cache_root),
        image_mount_root=str(mount_root),
        publish_source_to_cache=False,
    )

    result = loader.load_image(
        ContainerRequestContext(
            container_id="ctr-1",
            image_id="image-1",
            archive_sha256=authorized_sha256,
        )
    )

    assert result.loaded
    assert [request.archive_path for request in source_loader.requests] == [str(archive_path)]
    assert archive_path.read_bytes() == authorized
    assert [request.archive_sha256 for request in mounter.requests] == [authorized_sha256]
