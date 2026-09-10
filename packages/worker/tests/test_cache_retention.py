from __future__ import annotations

import os
from datetime import timedelta
from pathlib import Path

from shared.timestamps import utc_now
from worker.container_service.models import WorkerContainerServiceInstance
from worker.container_service.state import LocalWorkerContainerInstanceStore
from worker.retention import (
    WorkerRetentionConfig,
    WorkerRetentionService,
)


def test_worker_retention_bounds_caches_and_preserves_active_images(
    tmp_path: Path,
) -> None:
    now = utc_now()
    image_cache = tmp_path / "images"
    image_mounts = tmp_path / "mounts"
    checkpoints = tmp_path / "checkpoints"
    for root in (image_cache, image_mounts, checkpoints):
        root.mkdir()
    active_archive = image_cache / "active.rclip"
    stale_archive = image_cache / "stale.rclip"
    active_archive.write_bytes(b"active-image")
    stale_archive.write_bytes(b"stale-image")
    active_mount = image_mounts / "active"
    stale_mount = image_mounts / "stale"
    active_mount.mkdir()
    stale_mount.mkdir()
    (active_mount / "rootfs").write_bytes(b"active-root")
    (stale_mount / "rootfs").write_bytes(b"stale-root")
    stale_checkpoint = checkpoints / "checkpoint-stale"
    stale_checkpoint.mkdir()
    (stale_checkpoint / "state").write_bytes(b"checkpoint")
    old = (now - timedelta(days=30)).timestamp()
    for path in (
        active_archive,
        stale_archive,
        active_mount,
        stale_mount,
        stale_checkpoint,
    ):
        os.utime(path, (old, old))

    instances = LocalWorkerContainerInstanceStore()
    instances.save_container_instance(
        WorkerContainerServiceInstance(
            container_id="container-active",
            root_path=str(tmp_path / "container"),
            image_id="active",
        )
    )
    result = WorkerRetentionService(
        instances=instances,
        config=WorkerRetentionConfig(
            image_cache_root=image_cache,
            image_mount_root=image_mounts,
            checkpoint_root=checkpoints,
            image_cache_max_bytes=8,
            image_materialization_max_bytes=8,
            checkpoint_cache_max_bytes=8,
            recent_guard_seconds=0,
            materialization_retention_seconds=1,
            checkpoint_retention_seconds=1,
        ),
    ).reconcile(now=now)

    assert active_archive.exists()
    assert active_mount.exists()
    assert not stale_archive.exists()
    assert not stale_mount.exists()
    assert not stale_checkpoint.exists()
    assert result.active_image_count == 1
    assert result.removed == 3
    assert result.freed_bytes > 0
