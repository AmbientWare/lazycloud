from __future__ import annotations

import os
from datetime import timedelta
from pathlib import Path

import pytest
from shared.timestamps import utc_now
from worker.image_build_scratch import (
    ImageBuildScratchCapacityError,
    ImageBuildScratchManager,
)


def _buildah_path(_binary: str) -> str:
    return "/usr/bin/buildah"


def _manager(
    root: Path,
    *,
    minimum_free_bytes: int = 0,
    stale_seconds: int = 60,
) -> ImageBuildScratchManager:
    return ImageBuildScratchManager(
        root=root,
        worker_id="worker-1",
        max_bytes=32 * 1024 * 1024,
        per_build_max_bytes=16 * 1024 * 1024,
        minimum_free_bytes=minimum_free_bytes,
        stale_seconds=stale_seconds,
        buildah_binary="missing-buildah",
    )


def test_scratch_admission_reserves_bounded_isolated_build_roots(tmp_path: Path) -> None:
    manager = _manager(tmp_path / "builds")
    first = manager.acquire(build_id="build-1", container_id="container-1")
    second = manager.acquire(build_id="build-2", container_id="container-2")

    assert first.root != second.root
    assert first.root.parent == manager.root
    assert second.root.parent == manager.root
    assert first.root.name.startswith("image-build-worker-1-build-1-container-1-")
    with pytest.raises(ImageBuildScratchCapacityError, match="capacity is exhausted"):
        manager.acquire(build_id="build-3", container_id="container-3")

    manager.release(first)
    manager.release(second)
    assert not list(manager.root.glob("image-build-*"))


def test_reconcile_skips_locked_build_then_removes_interrupted_build(tmp_path: Path) -> None:
    manager = _manager(tmp_path / "builds", stale_seconds=30)
    lease = manager.acquire(build_id="build-1", container_id="container-1")
    old = (utc_now() - timedelta(minutes=5)).timestamp()
    os.utime(lease.root, (old, old))

    active = manager.reconcile()

    assert active.active == 1
    assert active.removed == 0
    lease.close()
    os.utime(lease.root, (old, old))

    removed = manager.reconcile()

    assert removed.active == 0
    assert removed.removed == 1
    assert not lease.root.exists()


def test_release_closes_lease_when_build_root_disappeared(tmp_path: Path) -> None:
    manager = _manager(tmp_path / "builds")
    lease = manager.acquire(build_id="build-1", container_id="container-1")
    lease_file = lease._lease_file
    lease.root.rename(tmp_path / "externally-moved-build")

    with pytest.raises(FileNotFoundError):
        manager.release(lease)

    assert lease_file.closed
