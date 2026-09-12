from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

from cache.server import FileCacheServer
from worker.image_archive_cache import restore_image_archive_from_content_cache
from worker.image_lifecycle import (
    ImageArchiveStorageMode,
    RestoredImageArchiveValidation,
    validate_restored_image_archive,
)
from worker.image_mount import (
    ImageArchiveContentCacheRestorePlan,
    plan_image_archive_content_cache_restore,
)


def test_concurrent_image_restores_publish_complete_archives(tmp_path: Path) -> None:
    cache = FileCacheServer(root=tmp_path / "content")
    cache.prepare()
    payload = b"shared image archive" * 1024
    stored = cache.store_content_bytes(payload)
    assert stored.stored
    archive_path = tmp_path / "images" / "shared.rclip"
    plan = plan_image_archive_content_cache_restore(
        archive_path=str(archive_path),
        image_id="shared",
        content_hash=stored.content_hash,
        size_bytes=len(payload),
        chunk_size_bytes=1024,
    )
    downloads_complete = Barrier(2, timeout=5)

    def validate(
        path: Path, plan: ImageArchiveContentCacheRestorePlan
    ) -> RestoredImageArchiveValidation:
        assert path.read_bytes() == payload
        downloads_complete.wait()
        return validate_restored_image_archive(
            image_id=plan.image_id,
            size_bytes=path.stat().st_size,
            metadata_valid=True,
            storage_mode=ImageArchiveStorageMode.Local,
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        restores = [
            executor.submit(
                restore_image_archive_from_content_cache, cache, plan, validator=validate
            )
            for _ in range(2)
        ]
        for restore in restores:
            result = restore.result(timeout=10)
            assert result.complete
            assert result.actual_hash == stored.content_hash

    assert archive_path.read_bytes() == payload
    assert list(archive_path.parent.iterdir()) == [archive_path]
