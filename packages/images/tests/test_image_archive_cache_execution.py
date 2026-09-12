from __future__ import annotations

import hashlib
from pathlib import Path

from cache.protocol import CacheContentReadRequest, CacheContentReadStatus
from cache.server import FileCacheServer
from tests.cache_fakes import empty_cache_disk_usage
from worker.image_archive_cache import (
    ImageArchiveContentCachePublishExecutionStatus,
    ImageArchiveContentCacheRestoreExecutionStatus,
    WorkerImageArchiveLoadStatus,
    WorkerImageArchiveLocalState,
    load_image_archive_from_cache_or_source,
    publish_image_archive_to_content_cache,
    restore_image_archive_from_content_cache,
)
from worker.image_lifecycle import (
    ImageArchiveStorageMode,
    validate_restored_image_archive,
)
from worker.image_mount import (
    plan_embedded_image_archive_cache_publish,
    plan_image_archive_content_cache_restore,
)


def test_restore_image_archive_from_content_cache_writes_validates_and_renames(
    tmp_path: Path,
) -> None:
    cache = FileCacheServer(tmp_path / "cache", disk_usage_reader=empty_cache_disk_usage)
    payload = b"archive-data-123"
    content_hash = hashlib.sha256(payload).hexdigest()
    cache.store_content_bytes(payload, expected_hash=content_hash)
    archive_path = tmp_path / "images" / "image-a.rclip"
    plan = plan_image_archive_content_cache_restore(
        archive_path=str(archive_path),
        image_id="image-a",
        content_hash=content_hash,
        size_bytes=len(payload),
        routing_key="/images/image-a.rclip",
        chunk_size_bytes=5,
    )

    result = restore_image_archive_from_content_cache(
        cache,
        plan,
        validator=lambda path, restore_plan: validate_restored_image_archive(
            image_id=restore_plan.image_id,
            size_bytes=path.stat().st_size,
            metadata_valid=True,
            storage_mode=ImageArchiveStorageMode.Local,
        ),
    )

    assert result.status is ImageArchiveContentCacheRestoreExecutionStatus.Complete
    assert result.complete
    assert result.bytes_written == len(payload)
    assert archive_path.read_bytes() == payload
    assert set(archive_path.parent.iterdir()) == {archive_path}


def test_restore_image_archive_from_content_cache_cleans_up_read_and_validation_failures(
    tmp_path: Path,
) -> None:
    cache = FileCacheServer(tmp_path / "cache", disk_usage_reader=empty_cache_disk_usage)
    archive_path = tmp_path / "images" / "image-a.rclip"
    missing = plan_image_archive_content_cache_restore(
        archive_path=str(archive_path),
        image_id="image-a",
        content_hash=hashlib.sha256(b"missing").hexdigest(),
        size_bytes=10,
    )

    missing_result = restore_image_archive_from_content_cache(
        cache,
        missing,
        validator=lambda _path, _plan: raise_unexpected_validation(),
    )
    assert missing_result.status is ImageArchiveContentCacheRestoreExecutionStatus.Error
    assert "image archive cache read failed" in missing_result.reason
    assert not archive_path.exists()
    assert list(archive_path.parent.iterdir()) == []

    payload = b"oci-archive"
    content_hash = hashlib.sha256(payload).hexdigest()
    cache.store_content_bytes(payload, expected_hash=content_hash)
    invalid = plan_image_archive_content_cache_restore(
        archive_path=str(archive_path),
        image_id="image-a",
        content_hash=content_hash,
        size_bytes=len(payload),
    )
    invalid_result = restore_image_archive_from_content_cache(
        cache,
        invalid,
        validator=lambda _path, restore_plan: validate_restored_image_archive(
            image_id=restore_plan.image_id,
            size_bytes=restore_plan.size_bytes,
            metadata_valid=True,
            storage_mode=ImageArchiveStorageMode.Oci,
            has_image_metadata=False,
            layer_count=1,
            decompressed_hash_count=1,
        ),
    )
    assert invalid_result.status is ImageArchiveContentCacheRestoreExecutionStatus.Error
    assert invalid_result.reason == "restored v2 image archive is missing embedded image metadata"
    assert not archive_path.exists()
    assert list(archive_path.parent.iterdir()) == []


def test_publish_image_archive_to_content_cache_stores_and_reports_failures(
    tmp_path: Path,
) -> None:
    cache = FileCacheServer(tmp_path / "cache", disk_usage_reader=empty_cache_disk_usage)
    archive_path = tmp_path / "image-a.rclip"
    archive_path.write_bytes(b"archive")
    publish = plan_embedded_image_archive_cache_publish(
        archive_path=str(archive_path),
        image_id="image-a",
        cache_client_available=True,
    )

    result = publish_image_archive_to_content_cache(cache, publish)

    assert result.status is ImageArchiveContentCachePublishExecutionStatus.Complete
    assert result.complete
    assert result.bytes_stored == len(b"archive")
    read = cache.read_content(
        CacheContentReadRequest(content_hash=result.content_hash, length=result.bytes_stored)
    )
    assert read.status is CacheContentReadStatus.Hit
    assert read.data == b"archive"

    skipped = publish_image_archive_to_content_cache(
        cache,
        plan_embedded_image_archive_cache_publish(
            archive_path=str(archive_path),
            image_id="image-a",
            cache_client_available=False,
        ),
    )
    assert skipped.status is ImageArchiveContentCachePublishExecutionStatus.Skipped

    missing = publish_image_archive_to_content_cache(
        cache,
        plan_embedded_image_archive_cache_publish(
            archive_path=str(tmp_path / "missing.rclip"),
            image_id="image-a",
            cache_client_available=True,
        ),
    )
    assert missing.status is ImageArchiveContentCachePublishExecutionStatus.Error
    assert missing.reason == "image archive source is missing"


def test_load_image_archive_uses_local_ready_then_content_cache_then_source_fallback(
    tmp_path: Path,
) -> None:
    cache = FileCacheServer(tmp_path / "cache", disk_usage_reader=empty_cache_disk_usage)
    archive_path = tmp_path / "images" / "image-a.rclip"

    local_ready = load_image_archive_from_cache_or_source(
        cache,
        archive_path=str(archive_path),
        image_id="image-a",
        cache_path="/images/image-a.rclip",
        local_state=WorkerImageArchiveLocalState(exists=True, size_bytes=10),
        validator=lambda _path, _plan: raise_unexpected_validation(),
    )
    assert local_ready.status is WorkerImageArchiveLoadStatus.LocalReady
    assert not local_ready.should_pull_source

    payload = b"archive-data"
    content_hash = hashlib.sha256(payload).hexdigest()
    cache.store_content_bytes(payload, expected_hash=content_hash)
    restored = load_image_archive_from_cache_or_source(
        cache,
        archive_path=str(archive_path),
        image_id="image-a",
        cache_path="/images/image-a.rclip",
        metadata_hash=content_hash,
        metadata_size_bytes=len(payload),
        cached_reachable=True,
        validator=lambda path, restore_plan: validate_restored_image_archive(
            image_id=restore_plan.image_id,
            size_bytes=path.stat().st_size,
            metadata_valid=True,
            storage_mode=ImageArchiveStorageMode.Local,
        ),
    )
    assert restored.status is WorkerImageArchiveLoadStatus.RestoredFromContentCache
    assert restored.restore is not None
    assert restored.restore.complete
    assert archive_path.read_bytes() == payload

    fallback = load_image_archive_from_cache_or_source(
        cache,
        archive_path=str(tmp_path / "images" / "image-b.rclip"),
        image_id="image-b",
        cache_path="/images/image-b.rclip",
        metadata_error="cache.ErrContentNotFound",
        validator=lambda _path, _plan: raise_unexpected_validation(),
    )
    assert fallback.status is WorkerImageArchiveLoadStatus.SourceFallback
    assert fallback.should_pull_source
    assert fallback.copy_plan is not None
    assert fallback.reason == "embedded cache metadata miss"


def raise_unexpected_validation():
    raise AssertionError("validation should not run")
