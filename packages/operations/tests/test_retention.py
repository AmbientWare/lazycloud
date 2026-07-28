from __future__ import annotations

import os
import threading
from datetime import datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from api.server.services import ApiServices
from control.service import ControlPlaneService
from database.repositories.cleanup import CleanupRepository
from database.repositories.identity import WorkspaceRepository
from database.repositories.images import CheckpointRepository, ImageBuildRepository, ImageRepository
from database.repositories.source_cache import SourceCacheCleanupRepository
from database.repositories.storage import (
    ObjectReferenceRepository,
    ObjectRepository,
)
from shared.app_identity import SOURCE_PACKAGE_BUCKET
from shared.checkpoints import CheckpointRecord, CheckpointStatus, checkpoint_recent_stub_key
from shared.errors import ConflictError, NotFoundError
from shared.identity import WorkspaceStatus
from shared.image_building.authoring import ImageSpec
from shared.image_building.records import (
    BuildStatus,
    ImageBuildPhase,
    ImageBuildRecord,
    ImageRecord,
)
from shared.objects import ObjectWriteCommand
from shared.source_cache_cleanup import SourceCacheCleanupStatus
from shared.timestamps import utc_now
from shared.workload_config import StubConfig, StubImageConfig
from sqlalchemy import event
from storage.checkpoint_retention import DurableCheckpointRetentionService
from storage.retention import RetentionConfig, RetentionService
from storage.service import (
    OBJECT_SHA256_METADATA_KEY,
    CacheStorage,
    MountedCacheClient,
    MountedCacheSettings,
    ObjectStorage,
)
from storage_client.s3 import S3ObjectInfo
from worker.checkpoints import (
    WorkerCheckpointStatus,
    create_checkpoint_state_payload,
    mark_checkpoint_restored_payload,
    update_checkpoint_status_payload,
)
from worker.container_service.models import WorkerContainerServiceInstance
from worker.container_service.state import LocalWorkerContainerInstanceStore
from worker.retention import (
    WorkerRetentionConfig,
    WorkerRetentionService,
)
from worker_repository.checkpoint_records import CheckpointService


class _MemoryObjectClient:
    def __init__(self) -> None:
        self.objects: dict[tuple[str, str], bytes] = {}
        self.object_metadata: dict[tuple[str, str], dict[str, str]] = {}

    def put_bytes(
        self,
        key: str,
        data: bytes,
        *,
        bucket: str | None = None,
        content_type: str = "application/octet-stream",
        metadata: dict[str, str] | None = None,
    ) -> S3ObjectInfo:
        _ = content_type
        location = (bucket or "objects", key)
        self.objects[location] = data
        self.object_metadata[location] = dict(metadata or {})
        return S3ObjectInfo(
            bucket=location[0],
            key=key,
            size=len(data),
            metadata=dict(metadata or {}),
        )

    def put_file(
        self,
        key: str,
        source: str | Path,
        *,
        bucket: str | None = None,
        content_type: str = "application/octet-stream",
        metadata: dict[str, str] | None = None,
    ) -> S3ObjectInfo:
        return self.put_bytes(
            key,
            Path(source).read_bytes(),
            bucket=bucket,
            content_type=content_type,
            metadata=metadata,
        )

    def read_bytes(self, key: str, *, bucket: str | None = None) -> bytes:
        return self.objects[(bucket or "objects", key)]

    def download_file(
        self,
        key: str,
        target: str | Path,
        *,
        bucket: str | None = None,
    ) -> S3ObjectInfo:
        data = self.read_bytes(key, bucket=bucket)
        Path(target).write_bytes(data)
        return S3ObjectInfo(bucket=bucket or "objects", key=key, size=len(data))

    def exists(self, key: str, *, bucket: str | None = None) -> bool:
        return (bucket or "objects", key) in self.objects

    def head(self, key: str, *, bucket: str | None = None) -> S3ObjectInfo:
        data = self.read_bytes(key, bucket=bucket)
        location = (bucket or "objects", key)
        return S3ObjectInfo(
            bucket=location[0],
            key=key,
            size=len(data),
            metadata=self.object_metadata.get(location, {}),
        )

    def generate_presigned_get_url(
        self,
        key: str,
        *,
        bucket: str | None = None,
        expires_seconds: int = 3600,
    ) -> str:
        return f"memory://{bucket or 'objects'}/{key}?expires={expires_seconds}"

    def generate_presigned_put_url(
        self,
        key: str,
        *,
        bucket: str | None = None,
        expires_seconds: int = 3600,
        content_length: int = 0,
        content_type: str = "application/octet-stream",
    ) -> str:
        _ = (content_length, content_type)
        return f"memory://{bucket or 'objects'}/{key}?expires={expires_seconds}"

    def delete(self, key: str, *, bucket: str | None = None) -> None:
        location = (bucket or "objects", key)
        self.objects.pop(location, None)
        self.object_metadata.pop(location, None)


class _BlockingDeleteObjectClient(_MemoryObjectClient):
    def __init__(self) -> None:
        super().__init__()
        self.delete_started = threading.Event()
        self.continue_delete = threading.Event()

    def delete(self, key: str, *, bucket: str | None = None) -> None:
        self.delete_started.set()
        if not self.continue_delete.wait(timeout=5):
            raise TimeoutError("blocking object deletion was not released")
        super().delete(key, bucket=bucket)


class _BlockingPutObjectClient(_MemoryObjectClient):
    def __init__(self) -> None:
        super().__init__()
        self.block_put = False
        self.put_started = threading.Event()
        self.continue_put = threading.Event()

    def put_bytes(
        self,
        key: str,
        data: bytes,
        *,
        bucket: str | None = None,
        content_type: str = "application/octet-stream",
        metadata: dict[str, str] | None = None,
    ) -> S3ObjectInfo:
        if self.block_put:
            self.put_started.set()
            if not self.continue_put.wait(timeout=5):
                raise TimeoutError("blocking object write was not released")
        return super().put_bytes(
            key,
            data,
            bucket=bucket,
            content_type=content_type,
            metadata=metadata,
        )


class _RetentionBatch:
    def __init__(self, removed: int) -> None:
        self.removed = removed


class _RecordingRetention:
    def __init__(self, removed: int) -> None:
        self.removed = removed
        self.calls: list[datetime | None] = []

    def reconcile(self, *, now: datetime | None = None) -> _RetentionBatch:
        self.calls.append(now)
        return _RetentionBatch(self.removed)


class _FailingRetention:
    def __init__(self) -> None:
        self.calls: list[datetime | None] = []

    def reconcile(self, *, now: datetime | None = None) -> _RetentionBatch:
        self.calls.append(now)
        raise RuntimeError("retention unavailable")


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


def test_durable_retention_prunes_only_unreferenced_production_artifacts(
    isolated_services: ApiServices,
    tmp_path: Path,
) -> None:
    now = utc_now()
    future = now + timedelta(days=30)
    client = _MemoryObjectClient()
    objects = ObjectStorage(
        isolated_services.context,
        object_client=client,
        default_bucket="objects",
    )
    cache = CacheStorage(
        isolated_services.context,
        cache_client=MountedCacheClient(MountedCacheSettings(root=tmp_path / "cache")),
    )
    control = ControlPlaneService(isolated_services.context)
    workspace = control.upsert_workspace("default")
    retained_source = objects.put_bytes_for_workspace(
        workspace_id=workspace.id,
        bucket=SOURCE_PACKAGE_BUCKET,
        key="sources/retained.zip",
        data=b"retained",
    )
    stale_source = objects.put_bytes_for_workspace(
        workspace_id=workspace.id,
        bucket=SOURCE_PACKAGE_BUCKET,
        key="sources/stale.zip",
        data=b"stale",
    )
    stub = control.create_stub(
        "retained-artifacts",
        workspace=workspace.id,
        config=StubConfig(
            object_id=retained_source.id,
            image=StubImageConfig(image_id="image-live"),
        ),
    )
    stale_checkpoint_stub = control.create_stub(
        "stale-checkpoint-artifacts",
        workspace=workspace.id,
    )
    isolated_services.apps.create("retained_artifacts", stub_id=stub.id, workspace=workspace.id)

    build_path = isolated_services.context.paths.root / "image-builds" / "stale.rclip"
    build_path.parent.mkdir(parents=True, exist_ok=True)
    build_path.write_bytes(b"build-artifact")
    stale_build = ImageBuildRecord(
        id=str(uuid4()),
        image=ImageSpec(image_id="image-stale"),
        fingerprint="stale-fingerprint",
        image_id="image-stale",
        status=BuildStatus.Complete,
        phase=ImageBuildPhase.Complete,
        artifact_path=str(build_path),
        created_at=now,
        finished_at=now,
    )
    live_archive = objects.put_bytes_for_workspace(
        workspace_id=workspace.id,
        bucket="objects",
        key="image-live.rclip",
        data=b"data",
    )
    stale_archive = objects.put_bytes_for_workspace(
        workspace_id=workspace.id,
        bucket="objects",
        key="image-stale.rclip",
        data=b"data",
    )
    with isolated_services.context.database.session() as session:
        ImageRepository(session).upsert(
            ImageRecord(
                workspace_id=workspace.id,
                image_id="image-live",
                archive_object_id=live_archive.id,
                archive_object_key=live_archive.key,
                archive_size_bytes=live_archive.size,
                archive_sha256=live_archive.sha256,
            )
        )
        ImageRepository(session).upsert(
            ImageRecord(
                workspace_id=workspace.id,
                image_id="image-stale",
                archive_object_id=stale_archive.id,
                archive_object_key=stale_archive.key,
                archive_size_bytes=stale_archive.size,
                archive_sha256=stale_archive.sha256,
            )
        )
        ImageBuildRepository(session).upsert(stale_build, workspace_id=workspace.id)
        CheckpointRepository(session).upsert(
            CheckpointRecord(
                checkpoint_id="checkpoint-live",
                workspace_id=workspace.id,
                stub_id=stub.id,
                status=CheckpointStatus.Available,
                origin_key="checkpoints/live.tar",
                retention_expires_at=now + timedelta(days=1),
            )
        )
        CheckpointRepository(session).upsert(
            CheckpointRecord(
                checkpoint_id="checkpoint-stale",
                workspace_id=workspace.id,
                stub_id=stale_checkpoint_stub.id,
                status=CheckpointStatus.Available,
                origin_key="checkpoints/stale.tar",
                retention_expires_at=now + timedelta(days=1),
            )
        )
    for key in (
        "checkpoints/live.tar",
        "checkpoints/stale.tar",
    ):
        objects.put_bytes_for_workspace(
            workspace_id=workspace.id,
            bucket="objects",
            key=key,
            data=b"data",
        )

    result = RetentionService(
        context=isolated_services.context,
        object_storage=objects,
        cache_storage=cache,
        config=RetentionConfig(
            image_archive_bucket="objects",
            checkpoint_bucket="objects",
            source_grace_seconds=1,
            build_retention_seconds=1,
            image_retention_seconds=1,
        ),
    ).reconcile(
        active_recent_stub_keys=[checkpoint_recent_stub_key(workspace.id, stub.id)],
        now=future,
    )

    assert objects.get_by_id(retained_source.id).id == retained_source.id
    with pytest.raises(NotFoundError, match=stale_source.id):
        objects.get_by_id(stale_source.id)
    live_physical_key = objects.physical_key_for_workspace(
        workspace.id,
        bucket="objects",
        key=live_archive.key,
    )
    stale_physical_key = objects.physical_key_for_workspace(
        workspace.id,
        bucket="objects",
        key=stale_archive.key,
    )
    physical_bucket = objects.physical_bucket("objects")
    assert client.exists(live_physical_key, bucket=physical_bucket)
    assert not client.exists(stale_physical_key, bucket=physical_bucket)
    assert client.exists(
        objects.physical_key_for_workspace(
            workspace.id,
            bucket="objects",
            key="checkpoints/live.tar",
        ),
        bucket=physical_bucket,
    )
    assert not client.exists(
        objects.physical_key_for_workspace(
            workspace.id,
            bucket="objects",
            key="checkpoints/stale.tar",
        ),
        bucket=physical_bucket,
    )
    assert not build_path.exists()
    with isolated_services.context.database.session() as session:
        assert ImageRepository(session).get("image-live", workspace_id=workspace.id) is not None
        assert ImageRepository(session).get("image-stale", workspace_id=workspace.id) is None
        assert ImageBuildRepository(session).get_across_workspaces(stale_build.id) is None
        assert CheckpointRepository(session).get_across_workspaces("checkpoint-live") is not None
        assert CheckpointRepository(session).get_across_workspaces("checkpoint-stale") is None
    assert result.source_objects_removed == 1
    assert result.image_records_removed == 1
    assert result.build_records_removed == 1
    assert result.checkpoints_removed == 1


def test_source_retention_preserves_cleanup_target_through_later_workspace_deletion(
    isolated_services: ApiServices,
    tmp_path: Path,
) -> None:
    now = utc_now()
    future = now + timedelta(days=30)
    client = _MemoryObjectClient()
    objects = ObjectStorage(
        isolated_services.context,
        object_client=client,
        default_bucket="objects",
    )
    workspace = ControlPlaneService(isolated_services.context).upsert_workspace(
        "retained-source-owner"
    )
    generation_id = str(uuid4())
    with isolated_services.context.database.session() as session:
        generation = SourceCacheCleanupRepository(session).register_generation(
            generation_id,
            worker_id="worker-1",
            storage_id="cache-storage-1",
            workspace_id=None,
            now=now,
        )
    source = objects.put_bytes_for_workspace(
        workspace_id=workspace.id,
        bucket=SOURCE_PACKAGE_BUCKET,
        key="sources/retention-before-workspace-delete.zip",
        data=b"cached source",
    )
    service = RetentionService(
        context=isolated_services.context,
        object_storage=objects,
        cache_storage=CacheStorage(
            isolated_services.context,
            cache_client=MountedCacheClient(MountedCacheSettings(root=tmp_path / "cache")),
        ),
        config=RetentionConfig(
            image_archive_bucket="objects",
            checkpoint_bucket="objects",
        ),
    )

    assert (
        service._prune_source_objects(
            frozenset(),
            created_before=future,
            recent_build_after=now,
        )
        == 1
    )
    with isolated_services.context.database.session() as session:
        targets = SourceCacheCleanupRepository(session).list_targets(workspace_id=workspace.id)
        assert ObjectRepository(session).get_owned(source.id, include_operations=True) is None
    assert len(targets) == 1
    assert targets[0].source_object_id == source.id
    assert targets[0].status is SourceCacheCleanupStatus.Pending

    with isolated_services.context.database.session() as session:
        workspaces = WorkspaceRepository(session)
        deleting = workspaces.lock_for_deletion(workspace.id)
        workspaces.mark_deleting(deleting)
        workspaces.purge_owned_records(workspace.id)
        deleted = workspaces.tombstone(deleting)
    assert deleted.status is WorkspaceStatus.Deleted

    with isolated_services.context.database.session() as session:
        cleanup = SourceCacheCleanupRepository(session)
        claimed = cleanup.claim_due(
            generation_id,
            worker_id="worker-1",
            session_fence=generation.session_fence,
            now=future,
            lease_until=future + timedelta(minutes=1),
            limit=10,
        )
        assert len(claimed) == 1
        assert claimed[0].claim_token is not None
        assert cleanup.complete_claim(
            claimed[0].id,
            generation_id=generation_id,
            worker_id="worker-1",
            session_fence=generation.session_fence,
            claim_token=claimed[0].claim_token,
            now=future,
        )
    with isolated_services.context.database.session() as session:
        summary = SourceCacheCleanupRepository(session).summarize(workspace_id=workspace.id)
    assert summary.complete
    assert summary.completed_count == 1


def test_retention_preserves_selected_archive_and_removes_loser(
    isolated_services: ApiServices,
    tmp_path: Path,
) -> None:
    now = utc_now()
    client = _MemoryObjectClient()
    objects = ObjectStorage(
        isolated_services.context,
        object_client=client,
        default_bucket="objects",
    )
    workspace = ControlPlaneService(isolated_services.context).upsert_workspace("default")
    image_id = "img_immutable"
    selected_key = f"image-builds/build-winner/{image_id}.rclip"
    stale_key = f"image-builds/build-stale/{image_id}.rclip"
    selected_archive = objects.put_bytes_for_workspace(
        workspace_id=workspace.id,
        bucket="objects",
        key=selected_key,
        data=b"winner",
    )
    objects.put_bytes_for_workspace(
        workspace_id=workspace.id,
        bucket="objects",
        key=stale_key,
        data=b"stale-owner",
    )
    with isolated_services.context.database.session() as session:
        ImageRepository(session).upsert(
            ImageRecord(
                workspace_id=workspace.id,
                image_id=image_id,
                archive_object_id=selected_archive.id,
                archive_object_key=selected_archive.key,
                archive_size_bytes=selected_archive.size,
                archive_sha256=selected_archive.sha256,
            )
        )
    service = RetentionService(
        context=isolated_services.context,
        object_storage=objects,
        cache_storage=CacheStorage(
            isolated_services.context,
            cache_client=MountedCacheClient(MountedCacheSettings(root=tmp_path / "cache")),
        ),
        config=RetentionConfig(
            image_archive_bucket="objects",
            checkpoint_bucket="objects",
            source_grace_seconds=1,
        ),
    )

    result = service.reconcile(
        active_recent_stub_keys=[],
        now=now + timedelta(seconds=2),
    )

    assert result.source_objects_removed == 1
    physical_bucket = objects.physical_bucket("objects")
    selected_physical_key = objects.physical_key_for_workspace(
        workspace.id,
        bucket="objects",
        key=selected_key,
    )
    stale_physical_key = objects.physical_key_for_workspace(
        workspace.id,
        bucket="objects",
        key=stale_key,
    )
    assert client.exists(selected_physical_key, bucket=physical_bucket)
    assert client.read_bytes(selected_physical_key, bucket=physical_bucket) == b"winner"
    assert not client.exists(stale_physical_key, bucket=physical_bucket)


def test_checkpoint_retention_survives_empty_hot_state_index(
    isolated_services: ApiServices,
) -> None:
    now = utc_now()
    client = _MemoryObjectClient()
    objects = ObjectStorage(
        isolated_services.context,
        object_client=client,
        default_bucket="objects",
    )
    workspace = ControlPlaneService(isolated_services.context).upsert_workspace("default")
    records = (
        CheckpointRecord(
            checkpoint_id="checkpoint-legacy",
            workspace_id=workspace.id,
            origin_key="checkpoints/legacy-retained.tar",
            status=CheckpointStatus.Available,
        ),
        CheckpointRecord(
            checkpoint_id="checkpoint-future",
            workspace_id=workspace.id,
            origin_key="checkpoints/future-retained.tar",
            status=CheckpointStatus.Available,
            retention_expires_at=now + timedelta(days=1),
        ),
        CheckpointRecord(
            checkpoint_id="checkpoint-expired",
            workspace_id=workspace.id,
            origin_key="checkpoints/expired.tar",
            status=CheckpointStatus.Available,
            retention_expires_at=now - timedelta(seconds=1),
        ),
    )
    with isolated_services.context.database.session() as session:
        repository = CheckpointRepository(session)
        for record in records:
            repository.upsert(record)
    for record in records:
        objects.reserve_for_workspace(
            workspace_id=workspace.id,
            bucket="objects",
            key=record.origin_key,
            size=4,
            sha256="0" * 64,
        )
        client.put_bytes(
            objects.physical_key_for_workspace(
                workspace.id,
                bucket="objects",
                key=record.origin_key,
            ),
            b"data",
            bucket=objects.physical_bucket("objects"),
        )

    result = DurableCheckpointRetentionService(
        context=isolated_services.context,
        object_storage=objects,
        checkpoint_bucket="objects",
    ).prune([], now=now)

    assert [checkpoint.checkpoint_id for checkpoint in result.pruned] == ["checkpoint-expired"]
    physical_bucket = objects.physical_bucket("objects")
    physical_keys = {
        record.checkpoint_id: objects.physical_key_for_workspace(
            workspace.id,
            bucket="objects",
            key=record.origin_key,
        )
        for record in records
    }
    assert client.exists(physical_keys["checkpoint-legacy"], bucket=physical_bucket)
    assert client.exists(physical_keys["checkpoint-future"], bucket=physical_bucket)
    assert not client.exists(physical_keys["checkpoint-expired"], bucket=physical_bucket)


def test_checkpoint_creation_and_restore_record_durable_retention_deadline(
    isolated_services: ApiServices,
) -> None:
    workspace = ControlPlaneService(isolated_services.context).upsert_workspace("default")
    service = CheckpointService(
        isolated_services.context,
        retention_seconds=60,
        restore_lease_seconds=1800,
    )
    pending = service.save_state(
        create_checkpoint_state_payload(
            checkpoint_id="checkpoint-deadline",
            source_container_id="container-deadline",
            status=WorkerCheckpointStatus.Pending,
            workspace_id=workspace.id,
        )
    )
    assert pending.retention_expires_at is None

    created = service.save_state(
        create_checkpoint_state_payload(
            checkpoint_id=pending.checkpoint_id,
            source_container_id="container-deadline",
            status=WorkerCheckpointStatus.Available,
            workspace_id=workspace.id,
        )
    )
    assert created.retention_expires_at is not None
    with isolated_services.context.database.session() as session:
        CheckpointRepository(session).upsert(
            created.model_copy(update={"retention_expires_at": utc_now() - timedelta(days=1)})
        )

    retained = service.get_for_restore(
        created.checkpoint_id,
        workspace_id=workspace.id,
    )

    assert retained.retention_expires_at is not None
    assert retained.retention_expires_at > utc_now() + timedelta(minutes=29)

    restored = service.save_state(mark_checkpoint_restored_payload(created.checkpoint_id))
    assert restored.status is CheckpointStatus.Available
    assert restored.retention_expires_at is not None
    assert utc_now() + timedelta(seconds=55) < restored.retention_expires_at
    assert restored.retention_expires_at < utc_now() + timedelta(seconds=65)

    with isolated_services.context.database.session() as session:
        CheckpointRepository(session).upsert(
            restored.model_copy(update={"retention_expires_at": utc_now() + timedelta(minutes=30)})
        )
    failed = service.save_state(
        update_checkpoint_status_payload(
            created.checkpoint_id,
            WorkerCheckpointStatus.RestoreFailed,
        )
    )
    assert failed.status is CheckpointStatus.RestoreFailed
    assert failed.retention_expires_at is not None
    assert utc_now() + timedelta(seconds=55) < failed.retention_expires_at
    assert failed.retention_expires_at < utc_now() + timedelta(seconds=65)


def test_source_and_image_candidates_recheck_references_before_physical_delete(
    isolated_services: ApiServices,
    tmp_path: Path,
) -> None:
    now = utc_now()
    future = now + timedelta(days=30)
    client = _MemoryObjectClient()
    objects = ObjectStorage(
        isolated_services.context,
        object_client=client,
        default_bucket="objects",
    )
    service = RetentionService(
        context=isolated_services.context,
        object_storage=objects,
        cache_storage=CacheStorage(
            isolated_services.context,
            cache_client=MountedCacheClient(MountedCacheSettings(root=tmp_path / "cache")),
        ),
        config=RetentionConfig(
            image_archive_bucket="objects",
            checkpoint_bucket="objects",
        ),
    )
    control = ControlPlaneService(isolated_services.context)
    workspace = control.upsert_workspace("default")
    source = objects.put_bytes_for_workspace(
        workspace_id=workspace.id,
        bucket=SOURCE_PACKAGE_BUCKET,
        key="sources/race.zip",
        data=b"source",
    )
    image = ImageRecord(workspace_id=workspace.id, image_id="image-race")
    with isolated_services.context.database.session() as session:
        ImageRepository(session).upsert(image)
        source_candidate = ObjectRepository(session).get_owned(source.id)
    assert source_candidate is not None
    client.put_bytes("image-race.rclip", b"image", bucket="objects")

    stub = control.create_stub(
        "race-reference",
        workspace=workspace.id,
        config=StubConfig(
            object_id=source.id,
            image=StubImageConfig(image_id=image.image_id),
        ),
    )
    isolated_services.apps.create("race_reference", stub_id=stub.id, workspace=workspace.id)

    assert not service._prune_source_candidate(
        source_candidate,
        created_before=future,
        recent_build_after=future,
    )
    assert service._prune_image_candidate(
        image,
        updated_before=future,
        recent_build_after=future,
    ) == (0, 0, 0, 0, 0)
    assert objects.get_by_id(source.id).id == source.id
    assert client.exists("image-race.rclip", bucket="objects")


def test_cleaned_image_tombstone_blocks_reference_until_republication(
    isolated_services: ApiServices,
    tmp_path: Path,
) -> None:
    future = utc_now() + timedelta(days=30)
    client = _MemoryObjectClient()
    objects = ObjectStorage(
        isolated_services.context,
        object_client=client,
        default_bucket="objects",
    )
    workspace = ControlPlaneService(isolated_services.context).upsert_workspace("default")
    archive = objects.put_bytes_for_workspace(
        workspace_id=workspace.id,
        bucket="objects",
        key="image-cleaned.rclip",
        data=b"image",
    )
    image = ImageRecord(
        workspace_id=workspace.id,
        image_id="image-cleaned",
        archive_object_id=archive.id,
        archive_object_key=archive.key,
        archive_size_bytes=archive.size,
        archive_sha256=archive.sha256,
    )
    with isolated_services.context.database.session() as session:
        ImageRepository(session).upsert(image)
    service = RetentionService(
        context=isolated_services.context,
        object_storage=objects,
        cache_storage=CacheStorage(
            isolated_services.context,
            cache_client=MountedCacheClient(MountedCacheSettings(root=tmp_path / "cache")),
        ),
        config=RetentionConfig(
            image_archive_bucket="objects",
            checkpoint_bucket="objects",
        ),
    )

    removed = service._prune_image_candidate(
        image,
        updated_before=future,
        recent_build_after=future,
    )

    assert removed[:2] == (1, 1)
    with isolated_services.context.database.session() as session:
        images = ImageRepository(session)
        assert images.get(image.image_id, workspace_id=workspace.id) is None
        cleaned = images.get(
            image.image_id,
            workspace_id=workspace.id,
            include_cleaned=True,
        )
    assert cleaned is not None
    assert cleaned.cleanup_completed_at is not None
    with pytest.raises(ConflictError, match="cleanup is in progress"):
        ControlPlaneService(isolated_services.context).create_stub(
            "cleaned-image",
            workspace=workspace.id,
            config=StubConfig(image=StubImageConfig(image_id=image.image_id)),
        )

    republished_archive = objects.put_bytes_for_workspace(
        workspace_id=workspace.id,
        bucket="objects",
        key="image-cleaned-v2.rclip",
        data=b"republished",
    )
    republished_image = image.model_copy(
        update={
            "archive_object_id": republished_archive.id,
            "archive_object_key": republished_archive.key,
            "archive_size_bytes": republished_archive.size,
            "archive_sha256": republished_archive.sha256,
        }
    )
    republished_after = utc_now()
    with isolated_services.context.database.session() as session:
        images = ImageRepository(session)
        republished = images.upsert(republished_image)
        immediately_eligible = ObjectReferenceRepository(session).list_image_cleanup_candidates(
            excluded_image_ids=frozenset(),
            updated_before=republished_after,
            recent_build_after=republished_after,
            limit=10,
        )
    assert republished.cleanup_completed_at is None
    assert immediately_eligible == []
    with isolated_services.context.database.session() as session:
        assert (
            ImageRepository(session).get(
                image.image_id,
                workspace_id=workspace.id,
            )
            is not None
        )


def test_duplicate_build_cleanup_preserves_shared_path_and_cache_key(
    isolated_services: ApiServices,
    tmp_path: Path,
) -> None:
    now = utc_now()
    workspace = ControlPlaneService(isolated_services.context).upsert_workspace("default")
    shared_path = isolated_services.context.paths.root / "image-builds" / "shared.rclip"
    shared_path.parent.mkdir(parents=True, exist_ok=True)
    shared_path.write_bytes(b"shared")
    cache = CacheStorage(
        isolated_services.context,
        cache_client=MountedCacheClient(MountedCacheSettings(root=tmp_path / "cache")),
    )
    source = tmp_path / "cache-source"
    source.write_bytes(b"cache")
    cache_record = cache.put("build", "shared", source)
    old_build = ImageBuildRecord(
        id=str(uuid4()),
        image=ImageSpec(image_id="image-shared"),
        fingerprint="old",
        image_id="image-shared",
        status=BuildStatus.Complete,
        phase=ImageBuildPhase.Complete,
        artifact_path=str(shared_path),
        cache_metadata={"cache_publish_key": cache_record.key},
        created_at=now - timedelta(days=30),
        finished_at=now - timedelta(days=30),
    )
    retained_build = old_build.model_copy(
        update={
            "id": str(uuid4()),
            "fingerprint": "new",
            "created_at": now,
            "finished_at": now,
        }
    )
    with isolated_services.context.database.session() as session:
        builds = ImageBuildRepository(session)
        builds.upsert(old_build, workspace_id=workspace.id)
        builds.upsert(retained_build, workspace_id=workspace.id)

    result = RetentionService(
        context=isolated_services.context,
        object_storage=ObjectStorage(
            isolated_services.context,
            object_client=_MemoryObjectClient(),
            default_bucket="objects",
        ),
        cache_storage=cache,
        config=RetentionConfig(
            image_archive_bucket="objects",
            checkpoint_bucket="objects",
            build_retention_seconds=7 * 24 * 60 * 60,
        ),
    ).reconcile(active_recent_stub_keys=[], now=now)

    with isolated_services.context.database.session() as session:
        builds = ImageBuildRepository(session)
        assert builds.get_across_workspaces(old_build.id) is None
        assert builds.get_across_workspaces(retained_build.id) is not None
    assert shared_path.exists()
    assert cache.list() == [cache_record]
    assert result.build_records_removed == 1
    assert result.build_paths_removed == 0
    assert result.cache_entries_removed == 0


def test_build_retention_age_starts_when_the_build_finishes(
    isolated_services: ApiServices,
) -> None:
    now = utc_now()
    workspace = ControlPlaneService(isolated_services.context).upsert_workspace("default")
    old_finished = ImageBuildRecord(
        id=str(uuid4()),
        image=ImageSpec(image_id="old-finished"),
        fingerprint="old-finished",
        image_id="old-finished",
        status=BuildStatus.Complete,
        phase=ImageBuildPhase.Complete,
        created_at=now - timedelta(days=30),
        finished_at=now - timedelta(days=30),
    )
    just_finished = old_finished.model_copy(
        update={
            "id": str(uuid4()),
            "image": ImageSpec(image_id="just-finished"),
            "fingerprint": "just-finished",
            "image_id": "just-finished",
            "finished_at": now,
        }
    )
    with isolated_services.context.database.session() as session:
        builds = ImageBuildRepository(session)
        builds.upsert(old_finished, workspace_id=workspace.id)
        builds.upsert(just_finished, workspace_id=workspace.id)
        candidates = ObjectReferenceRepository(session).list_build_cleanup_candidates(
            excluded_build_ids=frozenset(),
            finished_before=now - timedelta(seconds=1),
            recent_build_after=now - timedelta(seconds=1),
            limit=10,
        )

    assert [build.id for build in candidates] == [old_finished.id]


def test_image_cleanup_drains_high_cardinality_builds_in_bounded_batches(
    isolated_services: ApiServices,
    tmp_path: Path,
) -> None:
    now = utc_now()
    workspace = ControlPlaneService(isolated_services.context).upsert_workspace("default")
    image = ImageRecord(workspace_id=workspace.id, image_id="image-many-builds")
    shared_path = isolated_services.context.paths.root / "image-builds" / "many-shared.rclip"
    shared_path.parent.mkdir(parents=True, exist_ok=True)
    shared_path.write_bytes(b"shared-build-artifact")
    with isolated_services.context.database.session() as session:
        ImageRepository(session).upsert(image)
        builds = ImageBuildRepository(session)
        for index in range(31):
            builds.upsert(
                ImageBuildRecord(
                    id=str(uuid4()),
                    image=ImageSpec(image_id=image.image_id),
                    fingerprint=f"many-builds-{index}",
                    image_id=image.image_id,
                    status=BuildStatus.Complete,
                    phase=ImageBuildPhase.Complete,
                    artifact_path=str(shared_path),
                    created_at=now - timedelta(days=30),
                    finished_at=now - timedelta(days=30),
                ),
                workspace_id=workspace.id,
            )
    service = RetentionService(
        context=isolated_services.context,
        object_storage=ObjectStorage(
            isolated_services.context,
            object_client=_MemoryObjectClient(),
            default_bucket="objects",
        ),
        cache_storage=CacheStorage(
            isolated_services.context,
            cache_client=MountedCacheClient(MountedCacheSettings(root=tmp_path / "cache")),
        ),
        config=RetentionConfig(
            image_archive_bucket="objects",
            checkpoint_bucket="objects",
            max_items_per_cycle=7,
        ),
    )
    queries = 0

    def count_query(*_args: object) -> None:
        nonlocal queries
        queries += 1

    engine = isolated_services.context.database.engine
    event.listen(engine, "before_cursor_execute", count_query)
    try:
        first = service._prune_image_candidate(
            image,
            updated_before=now + timedelta(days=1),
            recent_build_after=now,
        )
    finally:
        event.remove(engine, "before_cursor_execute", count_query)

    assert first[1] == 0
    assert first[2] == 7
    assert queries <= 30
    assert shared_path.exists()
    with isolated_services.context.database.session() as session:
        builds = ImageBuildRepository(session)
        assert builds.count_for_image(image.image_id, workspace_id=workspace.id) == 24
        claimed_image = ImageRepository(session).get(
            image.image_id,
            workspace_id=workspace.id,
            include_cleaned=True,
        )
    assert claimed_image is not None
    assert claimed_image.cleanup_claimed_at is not None
    assert claimed_image.cleanup_completed_at is None

    removed_builds = first[2]
    removed_images = first[1]
    while removed_images == 0:
        batch = service._delete_claimed_image(claimed_image)
        removed_images += batch[1]
        removed_builds += batch[2]

    assert removed_builds == 31
    assert removed_images == 1
    assert not shared_path.exists()
    with isolated_services.context.database.session() as session:
        assert (
            ImageBuildRepository(session).count_for_image(
                image.image_id,
                workspace_id=workspace.id,
            )
            == 0
        )
        completed = ImageRepository(session).get(
            image.image_id,
            workspace_id=workspace.id,
            include_cleaned=True,
        )
    assert completed is not None
    assert completed.cleanup_claimed_at is None
    assert completed.cleanup_completed_at is not None


def test_resumed_image_cleanup_shares_one_build_budget_across_images(
    isolated_services: ApiServices,
    tmp_path: Path,
) -> None:
    now = utc_now()
    workspace = ControlPlaneService(isolated_services.context).upsert_workspace("default")
    image_ids = ("claimed-image-a", "claimed-image-b")
    with isolated_services.context.database.session() as session:
        images = ImageRepository(session)
        builds = ImageBuildRepository(session)
        claims = CleanupRepository(session)
        for image_id in image_ids:
            images.upsert(ImageRecord(workspace_id=workspace.id, image_id=image_id))
            for index in range(6):
                builds.upsert(
                    ImageBuildRecord(
                        id=str(uuid4()),
                        image=ImageSpec(image_id=image_id),
                        fingerprint=f"{image_id}-{index}",
                        image_id=image_id,
                        status=BuildStatus.Complete,
                        phase=ImageBuildPhase.Complete,
                        created_at=now - timedelta(days=30),
                    ),
                    workspace_id=workspace.id,
                )
            claims.mark_image_claimed(
                image_id,
                workspace_id=workspace.id,
                claimed_at=now,
            )
    service = RetentionService(
        context=isolated_services.context,
        object_storage=ObjectStorage(
            isolated_services.context,
            object_client=_MemoryObjectClient(),
            default_bucket="objects",
        ),
        cache_storage=CacheStorage(
            isolated_services.context,
            cache_client=MountedCacheClient(MountedCacheSettings(root=tmp_path / "cache")),
        ),
        config=RetentionConfig(
            image_archive_bucket="objects",
            checkpoint_bucket="objects",
            max_items_per_cycle=7,
        ),
    )

    removed = service._resume_claimed_images()

    assert removed[2] == 7
    with isolated_services.context.database.session() as session:
        builds = ImageBuildRepository(session)
        assert (
            sum(
                builds.count_for_image(image_id, workspace_id=workspace.id)
                for image_id in image_ids
            )
            == 5
        )


def test_build_resource_protection_uses_constant_query_count(
    isolated_services: ApiServices,
    tmp_path: Path,
) -> None:
    workspace = ControlPlaneService(isolated_services.context).upsert_workspace("default")
    shared_path = str(tmp_path / "shared.rclip")
    cache_key = "shared-published-cache"
    retained = ImageBuildRecord(
        id=str(uuid4()),
        image=ImageSpec(image_id="retained-query-count"),
        fingerprint="retained-query-count",
        image_id="retained-query-count",
        status=BuildStatus.Complete,
        phase=ImageBuildPhase.Complete,
        artifact_path=shared_path,
        cache_metadata={"cache_publish_key": cache_key},
    )
    with isolated_services.context.database.session() as session:
        ImageBuildRepository(session).upsert(retained, workspace_id=workspace.id)

    queries = 0

    def count_query(*_args: object) -> None:
        nonlocal queries
        queries += 1

    engine = isolated_services.context.database.engine
    event.listen(engine, "before_cursor_execute", count_query)
    try:
        with isolated_services.context.database.session() as session:
            protected_paths, protected_cache_keys = ImageBuildRepository(
                session
            ).protected_artifact_resources(
                deleting_build_ids={str(uuid4())},
                paths={shared_path}
                | {str(tmp_path / f"candidate-{index}") for index in range(100)},
                cache_keys={cache_key} | {f"cache-{index}" for index in range(100)},
            )
    finally:
        event.remove(engine, "before_cursor_execute", count_query)

    assert protected_paths == frozenset({shared_path})
    assert protected_cache_keys == frozenset({cache_key})
    assert queries == 2


def test_artifact_reference_age_starts_when_the_build_finishes(
    isolated_services: ApiServices,
) -> None:
    now = utc_now()
    recent_after = now - timedelta(minutes=1)
    workspace = ControlPlaneService(isolated_services.context).upsert_workspace("default")
    objects = ObjectStorage(
        isolated_services.context,
        object_client=_MemoryObjectClient(),
        default_bucket=SOURCE_PACKAGE_BUCKET,
    )
    context_object_id = objects.put_bytes_for_workspace(
        workspace_id=workspace.id,
        bucket=SOURCE_PACKAGE_BUCKET,
        key="sources/recently-published.zip",
        data=b"recent",
    ).id
    build = ImageBuildRecord(
        id=str(uuid4()),
        image=ImageSpec(
            context_object_id=context_object_id,
            image_id="recently-published-image",
        ),
        fingerprint="recently-published-image",
        image_id="recently-published-image",
        status=BuildStatus.Complete,
        phase=ImageBuildPhase.Complete,
        created_at=now - timedelta(days=30),
        finished_at=now,
    )
    unfinished_context_object_id = objects.put_bytes_for_workspace(
        workspace_id=workspace.id,
        bucket=SOURCE_PACKAGE_BUCKET,
        key="sources/missing-finished-at.zip",
        data=b"unfinished",
    ).id
    unfinished_terminal = build.model_copy(
        update={
            "id": str(uuid4()),
            "image": ImageSpec(
                context_object_id=unfinished_context_object_id,
                image_id="missing-finished-at-image",
            ),
            "fingerprint": "missing-finished-at-image",
            "image_id": "missing-finished-at-image",
            "finished_at": None,
        }
    )
    with isolated_services.context.database.session() as session:
        builds = ImageBuildRepository(session)
        builds.upsert(build, workspace_id=workspace.id)
        builds.upsert(unfinished_terminal, workspace_id=workspace.id)
        references = ObjectReferenceRepository(session)
        assert references.object_is_referenced(
            context_object_id,
            workspace_id=workspace.id,
            recent_build_after=recent_after,
        )
        assert references.image_is_referenced(
            build.image_id or "",
            workspace_id=workspace.id,
            recent_build_after=recent_after,
        )
        assert references.build_is_retained(
            build,
            workspace_id=workspace.id,
            recent_build_after=recent_after,
        )
        assert references.object_is_referenced(
            unfinished_context_object_id,
            workspace_id=workspace.id,
            recent_build_after=recent_after,
        )
        assert references.image_is_referenced(
            unfinished_terminal.image_id or "",
            workspace_id=workspace.id,
            recent_build_after=recent_after,
        )
        assert references.build_is_retained(
            unfinished_terminal,
            workspace_id=workspace.id,
            recent_build_after=recent_after,
        )


def test_build_candidate_rechecks_status_before_deleting_physical_data(
    isolated_services: ApiServices,
    tmp_path: Path,
) -> None:
    now = utc_now()
    workspace = ControlPlaneService(isolated_services.context).upsert_workspace("default")
    artifact = isolated_services.context.paths.root / "image-builds" / "claimed.rclip"
    artifact.parent.mkdir(parents=True, exist_ok=True)
    artifact.write_bytes(b"claimed")
    candidate = ImageBuildRecord(
        id=str(uuid4()),
        image=ImageSpec(image_id="image-claimed"),
        fingerprint="claimed",
        image_id="image-claimed",
        status=BuildStatus.Complete,
        phase=ImageBuildPhase.Complete,
        artifact_path=str(artifact),
        created_at=now - timedelta(days=30),
        finished_at=now - timedelta(days=30),
    )
    with isolated_services.context.database.session() as session:
        builds = ImageBuildRepository(session)
        builds.upsert(candidate, workspace_id=workspace.id)
        builds.upsert(
            candidate.model_copy(
                update={
                    "status": BuildStatus.Running,
                    "phase": ImageBuildPhase.Submitted,
                }
            ),
            workspace_id=workspace.id,
        )
    service = RetentionService(
        context=isolated_services.context,
        object_storage=ObjectStorage(
            isolated_services.context,
            object_client=_MemoryObjectClient(),
            default_bucket="objects",
        ),
        cache_storage=CacheStorage(
            isolated_services.context,
            cache_client=MountedCacheClient(MountedCacheSettings(root=tmp_path / "cache")),
        ),
        config=RetentionConfig(
            image_archive_bucket="objects",
            checkpoint_bucket="objects",
        ),
    )

    assert service._prune_build_candidate(
        candidate,
        finished_before=now,
        recent_build_after=now,
    ) == (0, 0, 0)
    assert artifact.exists()
    with isolated_services.context.database.session() as session:
        retained = ImageBuildRepository(session).get_across_workspaces(candidate.id)
    assert retained is not None
    assert retained.status is BuildStatus.Running


def test_source_cleanup_claim_survives_crash_and_rejects_new_reference(
    isolated_services: ApiServices,
    tmp_path: Path,
) -> None:
    now = utc_now()
    future = now + timedelta(days=30)
    client = _MemoryObjectClient()
    objects = ObjectStorage(
        isolated_services.context,
        object_client=client,
        default_bucket="objects",
    )
    control = ControlPlaneService(isolated_services.context)
    workspace = control.upsert_workspace("default")
    source = objects.put_bytes_for_workspace(
        workspace_id=workspace.id,
        bucket=SOURCE_PACKAGE_BUCKET,
        key="sources/crash.zip",
        data=b"source",
    )
    unbound_stub = control.create_stub(
        "claimed-binding",
        workspace=workspace.id,
        config=StubConfig(object_id=source.id),
    )
    with isolated_services.context.database.session() as session:
        candidate = ObjectRepository(session).get_owned(source.id)
    assert candidate is not None
    service = RetentionService(
        context=isolated_services.context,
        object_storage=objects,
        cache_storage=CacheStorage(
            isolated_services.context,
            cache_client=MountedCacheClient(MountedCacheSettings(root=tmp_path / "cache")),
        ),
        config=RetentionConfig(
            image_archive_bucket="objects",
            checkpoint_bucket="objects",
            source_grace_seconds=1,
        ),
    )

    claimed = service._claim_source_candidate(
        candidate,
        created_before=future,
        recent_build_after=future,
    )
    assert claimed is not None
    assert claimed.cleanup_claimed_at is not None
    with pytest.raises(ConflictError, match="cleanup is in progress"):
        isolated_services.apps.create(
            "claimed_binding",
            stub_id=unbound_stub.id,
            workspace=workspace.id,
        )
    with pytest.raises(ConflictError, match="cleanup is in progress"):
        control.create_stub(
            "claimed-source",
            workspace=workspace.id,
            config=StubConfig(object_id=source.id),
        )

    result = service.reconcile(active_recent_stub_keys=[], now=future)

    assert result.source_objects_removed == 1
    assert not client.exists("sources/crash.zip", bucket=SOURCE_PACKAGE_BUCKET)
    with pytest.raises(NotFoundError, match=source.id):
        objects.get_by_id(source.id)
    with pytest.raises(ConflictError, match="unavailable"):
        control.create_stub(
            "deleted-source",
            workspace=workspace.id,
            config=StubConfig(object_id=source.id),
        )


def test_slow_object_delete_does_not_block_unrelated_database_write(
    isolated_services: ApiServices,
    tmp_path: Path,
) -> None:
    future = utc_now() + timedelta(days=30)
    client = _BlockingDeleteObjectClient()
    objects = ObjectStorage(
        isolated_services.context,
        object_client=client,
        default_bucket="objects",
    )
    control = ControlPlaneService(isolated_services.context)
    workspace = control.upsert_workspace("default")
    source = objects.put_bytes_for_workspace(
        workspace_id=workspace.id,
        bucket=SOURCE_PACKAGE_BUCKET,
        key="sources/slow.zip",
        data=b"source",
    )
    with isolated_services.context.database.session() as session:
        candidate = ObjectRepository(session).get_owned(source.id)
    assert candidate is not None
    service = RetentionService(
        context=isolated_services.context,
        object_storage=objects,
        cache_storage=CacheStorage(
            isolated_services.context,
            cache_client=MountedCacheClient(MountedCacheSettings(root=tmp_path / "cache")),
        ),
        config=RetentionConfig(
            image_archive_bucket="objects",
            checkpoint_bucket="objects",
        ),
    )
    cleanup_errors: list[BaseException] = []

    def cleanup() -> None:
        try:
            service._prune_source_candidate(
                candidate,
                created_before=future,
                recent_build_after=future,
            )
        except BaseException as exc:
            cleanup_errors.append(exc)

    cleanup_thread = threading.Thread(target=cleanup)
    cleanup_thread.start()
    assert client.delete_started.wait(timeout=2)
    write_finished = threading.Event()

    def write_unrelated_workspace() -> None:
        control.upsert_workspace("unrelated-write")
        write_finished.set()

    write_thread = threading.Thread(target=write_unrelated_workspace)
    write_thread.start()
    assert write_finished.wait(timeout=2)
    client.continue_delete.set()
    cleanup_thread.join(timeout=5)
    write_thread.join(timeout=5)

    assert not cleanup_errors
    assert not cleanup_thread.is_alive()
    assert not write_thread.is_alive()


def test_object_delete_claim_does_not_block_another_workspace_location(
    isolated_services: ApiServices,
) -> None:
    client = _BlockingDeleteObjectClient()
    objects = ObjectStorage(
        isolated_services.context,
        object_client=client,
        default_bucket="objects",
    )
    control = ControlPlaneService(isolated_services.context)
    first = control.upsert_workspace("default")
    second = control.upsert_workspace("second-object-workspace")
    objects.put_bytes_for_workspace(
        workspace_id=first.id,
        bucket="objects",
        key="shared/location.bin",
        data=b"first",
    )
    errors: list[BaseException] = []

    def delete() -> None:
        try:
            objects.delete_for_workspace(
                workspace_id=first.id,
                bucket="objects",
                key="shared/location.bin",
            )
        except BaseException as exc:
            errors.append(exc)

    thread = threading.Thread(target=delete)
    thread.start()
    assert client.delete_started.wait(timeout=2)
    written = objects.put_bytes_for_workspace(
        workspace_id=second.id,
        bucket="objects",
        key="shared/location.bin",
        data=b"second",
    )
    client.continue_delete.set()
    thread.join(timeout=5)

    assert not errors
    assert not thread.is_alive()
    assert written.sha256


def test_object_write_claim_blocks_delete_without_holding_database_transaction(
    isolated_services: ApiServices,
) -> None:
    client = _BlockingPutObjectClient()
    objects = ObjectStorage(
        isolated_services.context,
        object_client=client,
        default_bucket="objects",
    )
    control = ControlPlaneService(isolated_services.context)
    workspace = control.upsert_workspace("default")
    client.block_put = True
    errors: list[BaseException] = []

    def write() -> None:
        try:
            objects.put_bytes_for_workspace(
                workspace_id=workspace.id,
                bucket="objects",
                key="slow/write.bin",
                data=b"content",
            )
        except BaseException as exc:
            errors.append(exc)

    thread = threading.Thread(target=write)
    thread.start()
    assert client.put_started.wait(timeout=2)
    control.upsert_workspace("write-does-not-block-database")
    with pytest.raises(ConflictError, match="write is in progress"):
        objects.delete_for_workspace(
            workspace_id=workspace.id,
            bucket="objects",
            key="slow/write.bin",
        )
    client.continue_put.set()
    thread.join(timeout=5)

    assert not errors
    assert not thread.is_alive()
    assert objects.get_for_workspace(
        workspace_id=workspace.id,
        bucket="objects",
        key="slow/write.bin",
    ).sha256


def test_stale_object_write_claim_finalizes_matching_atomic_upload(
    isolated_services: ApiServices,
) -> None:
    client = _MemoryObjectClient()
    objects = ObjectStorage(
        isolated_services.context,
        object_client=client,
        default_bucket="objects",
    )
    workspace = ControlPlaneService(isolated_services.context).upsert_workspace("default")
    physical_key = objects.physical_key_for_workspace(
        workspace.id,
        bucket="objects",
        key="crash/write.bin",
    )
    command = ObjectWriteCommand(
        bucket="objects",
        key="crash/write.bin",
        path=f"s3://objects/{physical_key}",
        size=7,
        sha256="a" * 64,
    )
    with isolated_services.context.database.session() as session:
        claim = ObjectRepository(session).begin_write(
            command,
            workspace_id=workspace.id,
            overwrite=True,
        )
    client.put_bytes(
        physical_key,
        b"content",
        bucket=objects.physical_bucket("objects"),
        metadata={OBJECT_SHA256_METADATA_KEY: "a" * 64},
    )

    assert (
        objects.reconcile_operations(
            now=utc_now() + timedelta(hours=3),
            lease_seconds=2 * 60 * 60,
            limit=10,
        )
        == 1
    )

    recovered = objects.get_by_id(claim.record.id)
    assert recovered.write_claimed_at is None
    assert recovered.sha256 == "a" * 64


def test_stale_object_operations_roll_back_missing_write_and_resume_delete(
    isolated_services: ApiServices,
) -> None:
    client = _MemoryObjectClient()
    objects = ObjectStorage(
        isolated_services.context,
        object_client=client,
        default_bucket="objects",
    )
    workspace = ControlPlaneService(isolated_services.context).upsert_workspace("default")
    missing_physical_key = objects.physical_key_for_workspace(
        workspace.id,
        bucket="objects",
        key="crash/missing.bin",
    )
    command = ObjectWriteCommand(
        bucket="objects",
        key="crash/missing.bin",
        path=f"s3://objects/{missing_physical_key}",
        size=7,
        sha256="b" * 64,
    )
    with isolated_services.context.database.session() as session:
        missing = ObjectRepository(session).begin_write(
            command,
            workspace_id=workspace.id,
            overwrite=True,
        )
    doomed = objects.put_bytes_for_workspace(
        workspace_id=workspace.id,
        bucket="objects",
        key="crash/delete.bin",
        data=b"delete",
    )
    stale = utc_now() - timedelta(hours=3)
    with isolated_services.context.database.session() as session:
        ObjectRepository(session).claim_delete(
            doomed.id,
            cleanup_kind="object-delete",
            claimed_at=stale,
        )
    with pytest.raises(NotFoundError, match=doomed.id):
        objects.get_by_id(doomed.id)
    with isolated_services.context.database.session() as session:
        abandoned_owned = ObjectRepository(session).get_owned(doomed.id, include_operations=True)
    assert abandoned_owned is not None
    abandoned = abandoned_owned.record
    assert abandoned.cleanup_kind == "object-delete"
    assert abandoned.cleanup_claimed_at == stale

    assert (
        objects.reconcile_operations(
            now=utc_now() + timedelta(hours=3),
            lease_seconds=2 * 60 * 60,
            limit=10,
        )
        == 2
    )

    with pytest.raises(NotFoundError, match=missing.record.id):
        objects.get_by_id(missing.record.id)
    with pytest.raises(NotFoundError, match=doomed.id):
        objects.get_by_id(doomed.id)
    assert not client.exists(
        objects.physical_key_for_workspace(
            workspace.id,
            bucket="objects",
            key="crash/delete.bin",
        ),
        bucket=objects.physical_bucket("objects"),
    )
