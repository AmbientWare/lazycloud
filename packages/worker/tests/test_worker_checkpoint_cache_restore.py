from __future__ import annotations

import hashlib
from pathlib import Path

from cache.protocol import (
    CacheContentReadRequest,
    CacheContentReadResult,
    CacheContentReadStatus,
    CacheContentStoreResult,
    CacheContentStoreStatus,
)
from shared.checkpoints import CheckpointRecord
from worker.checkpoint_restore import assemble_checkpoint_archive_from_cache


class _Cache:
    """Ranged content reads, standing in for FileCacheServer."""

    def __init__(self, blob: bytes | None) -> None:
        self.blob = blob

    def read_content(self, request: CacheContentReadRequest) -> CacheContentReadResult:
        if self.blob is None:
            return CacheContentReadResult(
                status=CacheContentReadStatus.Miss,
                content_hash=request.content_hash,
            )
        chunk = self.blob[request.offset : request.offset + request.length]
        return CacheContentReadResult(
            status=CacheContentReadStatus.Hit if chunk else CacheContentReadStatus.Miss,
            content_hash=request.content_hash,
            data=chunk,
            offset=request.offset,
            length=len(chunk),
        )

    def store_content_from_local_file(
        self,
        path: str | Path,
        *,
        expected_hash: str = "",
        cache_path: str = "",
    ) -> CacheContentStoreResult:
        _ = (path, cache_path)
        return CacheContentStoreResult(
            status=CacheContentStoreStatus.Stored,
            content_hash=expected_hash,
        )


def _record(payload: bytes) -> CheckpointRecord:
    return CheckpointRecord(
        checkpoint_id="ck-1",
        cache_hash=hashlib.sha256(payload).hexdigest(),
        cache_size_bytes=len(payload),
        origin_key="checkpoints/ck-1.tar",
    )


def test_checkpoint_archive_is_rebuilt_from_the_content_cache(tmp_path: Path) -> None:
    """A cached checkpoint must not be re-downloaded from object storage."""
    payload = b"checkpoint-archive-bytes" * 500
    archive = tmp_path / "archive.tar"

    assert assemble_checkpoint_archive_from_cache(_Cache(payload), _record(payload), archive)
    assert archive.read_bytes() == payload


def test_a_truncated_cache_entry_is_not_accepted_as_complete(tmp_path: Path) -> None:
    """A short read must report a miss, never a checkpoint missing its tail."""
    payload = b"checkpoint-archive-bytes" * 500

    assembled = assemble_checkpoint_archive_from_cache(
        _Cache(payload[:100]),
        _record(payload),
        tmp_path / "archive.tar",
    )

    assert not assembled


def test_a_cache_miss_falls_through_instead_of_failing_the_restore(tmp_path: Path) -> None:
    """A miss costs a download; it must not surface as a restore failure."""
    payload = b"checkpoint-archive-bytes" * 500

    assert not assemble_checkpoint_archive_from_cache(
        _Cache(None), _record(payload), tmp_path / "archive.tar"
    )
