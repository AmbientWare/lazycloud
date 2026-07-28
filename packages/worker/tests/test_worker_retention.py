from __future__ import annotations

import os
import threading
from datetime import timedelta
from pathlib import Path

from shared.timestamps import utc_now
from worker.checkpoint_activity import CheckpointArtifactLease, CheckpointLeaseRegistry
from worker.container_service.models import WorkerContainerServiceInstance
from worker.image_build_scratch import ImageBuildScratchManager
from worker.retention import (
    WorkerRetentionConfig,
    WorkerRetentionService,
)


class _Instances:
    def list_container_instances(self) -> list[WorkerContainerServiceInstance]:
        return [
            WorkerContainerServiceInstance(
                container_id="container-active",
                root_path="/containers/active",
                image_id="active",
            )
        ]


def test_retention_preserves_active_local_clip_archive(tmp_path: Path) -> None:
    now = utc_now()
    image_cache = tmp_path / "images"
    image_mounts = tmp_path / "mounts"
    checkpoints = tmp_path / "checkpoints"
    for root in (image_cache, image_mounts, checkpoints):
        root.mkdir()

    active_archive = image_cache / "active.clip"
    inactive_archive = image_cache / "inactive.clip"
    active_archive.write_bytes(b"active")
    inactive_archive.write_bytes(b"inactive")
    old = (now - timedelta(days=1)).timestamp()
    os.utime(active_archive, (old, old))
    os.utime(inactive_archive, (old, old))

    result = WorkerRetentionService(
        instances=_Instances(),
        config=WorkerRetentionConfig(
            image_cache_root=image_cache,
            image_mount_root=image_mounts,
            checkpoint_root=checkpoints,
            image_cache_max_bytes=1,
            low_watermark_pct=1,
            recent_guard_seconds=0,
        ),
    ).reconcile(now=now)

    assert active_archive.exists()
    assert not inactive_archive.exists()
    assert result.image_cache_removed == 1


def test_retention_preserves_checkpoint_with_active_checkpoint_lease(tmp_path: Path) -> None:
    now = utc_now()
    image_cache = tmp_path / "images"
    image_mounts = tmp_path / "mounts"
    checkpoints = tmp_path / "checkpoints"
    for root in (image_cache, image_mounts, checkpoints):
        root.mkdir()
    active_checkpoint = checkpoints / "checkpoint-active"
    active_archive = checkpoints / "checkpoint-active.tar"
    active_extract = checkpoints / ".checkpoint-active.extract"
    expired_checkpoint = checkpoints / "checkpoint-expired"
    active_checkpoint.mkdir()
    active_extract.mkdir()
    expired_checkpoint.mkdir()
    (active_checkpoint / "data").write_bytes(b"active")
    active_archive.write_bytes(b"archive")
    (active_extract / "data").write_bytes(b"extracting")
    (expired_checkpoint / "data").write_bytes(b"expired")
    old = (now - timedelta(days=1)).timestamp()
    for path in (active_checkpoint, active_archive, active_extract):
        os.utime(path, (old, old))
    os.utime(expired_checkpoint, (old, old))
    activity = CheckpointLeaseRegistry()
    lease = activity.acquire(active_checkpoint.name)
    retention = WorkerRetentionService(
        instances=_Instances(),
        config=WorkerRetentionConfig(
            image_cache_root=image_cache,
            image_mount_root=image_mounts,
            checkpoint_root=checkpoints,
            checkpoint_cache_max_bytes=1,
            low_watermark_pct=1,
            recent_guard_seconds=0,
            checkpoint_retention_seconds=1,
        ),
        checkpoint_activity=activity,
    )

    result = retention.reconcile(now=now)

    assert active_checkpoint.exists()
    assert active_archive.exists()
    assert active_extract.exists()
    assert not expired_checkpoint.exists()
    assert result.checkpoints_removed == 1

    lease.close()
    released = retention.reconcile(now=now)

    assert not active_checkpoint.exists()
    assert not active_archive.exists()
    assert not active_extract.exists()
    assert released.checkpoints_removed == 3


def test_retention_guard_serializes_new_checkpoint_lease() -> None:
    activity = CheckpointLeaseRegistry()
    acquire_started = threading.Event()
    acquire_finished = threading.Event()
    acquired_lease: list[CheckpointArtifactLease] = []

    def acquire() -> None:
        acquire_started.set()
        acquired_lease.append(activity.acquire("checkpoint-active"))
        acquire_finished.set()

    with activity.retention_guard("checkpoint-active") as removable:
        assert removable
        thread = threading.Thread(target=acquire)
        thread.start()
        assert acquire_started.wait(timeout=1)
        assert not acquire_finished.wait(timeout=0.05)

    assert acquire_finished.wait(timeout=1)
    thread.join(timeout=1)
    assert not thread.is_alive()
    acquired_lease[0].close()


def test_retention_reclaims_interrupted_image_build_scratch_when_cache_pruning_disabled(
    tmp_path: Path,
) -> None:
    now = utc_now()
    image_cache = tmp_path / "images"
    image_mounts = tmp_path / "mounts"
    checkpoints = tmp_path / "checkpoints"
    for root in (image_cache, image_mounts, checkpoints):
        root.mkdir()
    scratch = ImageBuildScratchManager(
        root=tmp_path / "builds",
        worker_id="worker-1",
        max_bytes=32 * 1024 * 1024,
        per_build_max_bytes=16 * 1024 * 1024,
        minimum_free_bytes=0,
        stale_seconds=30,
        buildah_binary="missing-buildah",
    )
    interrupted = scratch.acquire(build_id="build-1", container_id="container-1")
    interrupted.close()
    old = (now - timedelta(minutes=5)).timestamp()
    os.utime(interrupted.root, (old, old))

    result = WorkerRetentionService(
        instances=_Instances(),
        config=WorkerRetentionConfig(
            image_cache_root=image_cache,
            image_mount_root=image_mounts,
            checkpoint_root=checkpoints,
            cache_pruning_enabled=False,
        ),
        image_build_scratch=scratch,
    ).reconcile(now=now)

    assert result.image_cache_scanned == 0
    assert result.image_build_scratch_scanned == 1
    assert result.image_build_scratch_removed == 1
    assert not interrupted.root.exists()
