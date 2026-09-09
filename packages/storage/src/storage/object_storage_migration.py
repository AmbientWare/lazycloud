from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from hashlib import file_digest
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import urlsplit

from database.repositories.object_storage_migration import (
    ObjectStorageMigrationRepository,
    ObjectStorageSnapshot,
)
from shared.app_identity import (
    IMAGE_BUILD_CONTEXT_BUCKET,
    SOURCE_PACKAGE_BUCKET,
    WORKSPACE_OBJECT_BUCKET,
)
from shared.identity import WorkspaceStorageConfig
from storage_client.s3 import S3ObjectInfo, S3ObjectStoreClient


@dataclass(frozen=True, slots=True)
class ObjectRelocation:
    id: str
    bucket: str
    path: str


@dataclass(slots=True)
class ObjectStorageMigrationPlan:
    buckets: dict[str, str]
    workspaces: dict[str, WorkspaceStorageConfig] = field(default_factory=dict, repr=False)
    objects: list[ObjectRelocation] = field(default_factory=list)
    archives: dict[str, str] = field(default_factory=dict)
    keys: dict[tuple[str, str], str] = field(default_factory=dict)
    required: dict[tuple[str, str], tuple[int, str]] = field(default_factory=dict)


@dataclass(slots=True)
class ObjectStorageMigration:
    source: S3ObjectStoreClient
    target: S3ObjectStoreClient

    def plan(self, snapshot: ObjectStorageSnapshot) -> ObjectStorageMigrationPlan:
        source = self.source.settings
        target = self.target.settings
        if source.endpoint_url == target.endpoint_url:
            raise RuntimeError("migration source and destination endpoints must differ")
        plan = ObjectStorageMigrationPlan({source.bucket: target.bucket})
        for workspace in snapshot.workspaces:
            storage = workspace.storage
            if storage.endpoint_url not in {source.endpoint_url, target.endpoint_url}:
                continue
            if storage.access_key or storage.secret_key:
                # An explicitly attached customer bucket retains its own authority.
                continue
            expected_bucket = f"{source.workspace_bucket_prefix}-{workspace.id}".replace("_", "-")
            target_bucket = f"{target.workspace_bucket_prefix}-{workspace.id}".replace("_", "-")
            migrated = storage.endpoint_url == target.endpoint_url
            if (
                storage.backend != "s3"
                or storage.bucket != (target_bucket if migrated else expected_bucket)
                or storage.prefix
                or storage.region != (target.region_name if migrated else source.region_name)
                or storage.config.get("session_token")
            ):
                raise RuntimeError(f"unknown managed storage coordinates: {workspace.id}")
            plan.buckets[expected_bucket] = target_bucket
            plan.workspaces[workspace.id] = WorkspaceStorageConfig(
                backend="s3",
                bucket=target_bucket,
                config={
                    "endpoint_url": target.endpoint_url,
                    "region": target.region_name,
                    "force_path_style": target.force_path_style,
                },
            )
        purposes = {WORKSPACE_OBJECT_BUCKET, IMAGE_BUILD_CONTEXT_BUCKET, SOURCE_PACKAGE_BUCKET}
        for owned in snapshot.objects:
            record = owned.record
            if record.bucket not in purposes | {source.bucket, target.bucket}:
                raise RuntimeError(f"unknown object bucket purpose: {record.id}")
            new_bucket = target.bucket if record.bucket == source.bucket else record.bucket
            old_bucket = source.bucket if record.bucket == target.bucket else record.bucket
            old_key = f"workspaces/{owned.workspace_id}/{old_bucket}/{record.key}"
            new_key = f"workspaces/{owned.workspace_id}/{new_bucket}/{record.key}"
            new_path = f"s3://{target.bucket}/{new_key}"
            if record.path not in {new_path, f"s3://{source.bucket}/{old_key}"}:
                raise RuntimeError(f"unknown object storage path: {record.id}")
            plan.keys[source.bucket, old_key] = new_key
            plan.required[source.bucket, old_key] = record.size, record.sha256
            plan.objects.append(ObjectRelocation(record.id, new_bucket, new_path))
        for archive in snapshot.archives:
            if archive.bucket not in {source.bucket, target.bucket}:
                raise RuntimeError(f"unknown image archive bucket: {archive.image_id}")
            plan.archives[archive.image_id] = target.bucket
            plan.required[source.bucket, archive.object_key] = archive.size_bytes, archive.sha256
        for path in snapshot.runtime_paths:
            location = urlsplit(path)
            if location.scheme in {"s3", "http", "https"}:
                raise RuntimeError(
                    "stored build paths contain remote URLs requiring explicit mapping"
                )
        return plan

    def copy_and_relocate(
        self,
        plan: ObjectStorageMigrationPlan,
        repository: ObjectStorageMigrationRepository,
        *,
        assert_writers_stopped: Callable[[], None],
        progress: Callable[[str], None],
    ) -> None:
        assert_writers_stopped()
        inventory: dict[tuple[str, str], S3ObjectInfo] = {}
        destinations: set[tuple[str, str]] = set()
        for bucket, target_bucket in plan.buckets.items():
            if self.source.has_multipart_uploads(bucket=bucket):
                raise RuntimeError(f"finish multipart uploads before migration: {bucket}")
            for listed in self.source.list_prefix("", bucket=bucket):
                source_key = bucket, listed.key
                destination = target_bucket, plan.keys.get(source_key, listed.key)
                if destination in destinations:
                    raise RuntimeError(f"migration maps multiple objects to {destination}")
                destinations.add(destination)
                inventory[source_key] = self.source.head(listed.key, bucket=bucket)
        missing = plan.required.keys() - inventory.keys()
        if missing:
            raise RuntimeError(
                f"durable storage references are missing {len(missing)} source objects"
            )
        with TemporaryDirectory(prefix="lazycloud-storage-migration-") as directory:
            local_source = Path(directory) / "source"
            local_target = Path(directory) / "target"
            for index, ((bucket, key), info) in enumerate(inventory.items(), start=1):
                self.source.download_file(key, local_source, bucket=bucket)
                digest = _digest(local_source)
                if self.source.head(key, bucket=bucket) != info:
                    raise RuntimeError(f"source object changed during migration: {bucket}/{key}")
                required = plan.required.get((bucket, key))
                if required is not None and required != (local_source.stat().st_size, digest):
                    raise RuntimeError(
                        f"source object disagrees with durable size or digest: {bucket}/{key}"
                    )
                target_bucket = plan.buckets[bucket]
                target_key = plan.keys.get((bucket, key), key)
                if not self.target.exists(target_key, bucket=target_bucket):
                    self.target.put_file(
                        target_key,
                        local_source,
                        bucket=target_bucket,
                        content_type=info.content_type,
                        metadata=info.metadata,
                        cache_control=info.cache_control,
                        content_disposition=info.content_disposition,
                        content_encoding=info.content_encoding,
                        content_language=info.content_language,
                        expires=info.expires,
                    )
                self.target.download_file(target_key, local_target, bucket=target_bucket)
                if _digest(local_target) != digest or _headers(
                    self.target.head(target_key, bucket=target_bucket)
                ) != _headers(info):
                    raise RuntimeError(
                        f"destination content or headers differ: {target_bucket}/{target_key}"
                    )
                progress(f"verified {index}/{len(inventory)} objects")
        assert_writers_stopped()
        for bucket in plan.buckets:
            keys = {item.key for item in self.source.list_prefix("", bucket=bucket)}
            expected = {key for source_bucket, key in inventory if source_bucket == bucket}
            if keys != expected or self.source.has_multipart_uploads(bucket=bucket):
                raise RuntimeError(f"source inventory changed during migration: {bucket}")
        for (bucket, key), info in inventory.items():
            if self.source.head(key, bucket=bucket) != info:
                raise RuntimeError(f"source object changed before metadata commit: {bucket}/{key}")
        for workspace_id, config in plan.workspaces.items():
            repository.relocate_workspace(workspace_id, config)
        for record in plan.objects:
            repository.relocate_object(record.id, bucket=record.bucket, path=record.path)
        for image_id, bucket in plan.archives.items():
            repository.relocate_archive(image_id, bucket=bucket)


def _digest(path: Path) -> str:
    with path.open("rb") as stream:
        return file_digest(stream, "sha256").hexdigest()


def _headers(info: S3ObjectInfo) -> S3ObjectInfo:
    return info.model_copy(update={"bucket": "", "key": "", "etag": None, "last_modified": None})
