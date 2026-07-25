from __future__ import annotations

import hashlib
import logging
import os
from collections.abc import Collection, Iterator
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Protocol
from uuid import uuid4

from database.repositories.artifact_cleanup import OBJECT_CLEANUP_DELETE
from database.repositories.storage import (
    CacheEntryRepository,
    ObjectRepository,
    ObjectWriteClaim,
)
from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from shared.app_identity import (
    ENV_PREFIX,
    IMAGE_BUILD_CONTEXT_BUCKET,
    SOURCE_PACKAGE_BUCKET,
    WORKSPACE_OBJECT_BUCKET,
)
from shared.cache_records import CacheEntry
from shared.errors import (
    ConflictError,
    InvalidInputError,
    NotFoundError,
    UpstreamUnavailableError,
)
from shared.objects import ObjectRecord, ObjectWriteCommand
from shared.paths import state_home
from shared.timestamps import utc_now
from storage_client.s3 import (
    S3ObjectInfo,
    S3ObjectStoreClient,
    S3ObjectStoreSettings,
    default_s3_object_store_client,
)

from storage.context import StorageContext

OBJECT_SHA256_METADATA_KEY = "artifact-sha256"
LOGGER = logging.getLogger(__name__)


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(8 * 1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _cache_key(namespace: str, key: str) -> str:
    return hashlib.sha256(f"{namespace}:{key}".encode()).hexdigest()


def _cache_object_key(cache_key: str, sha256: str) -> str:
    return f"{cache_key}{sha256}"


def _validate_cache_object_key(key: str) -> None:
    if len(key) not in {64, 128} or any(character not in "0123456789abcdef" for character in key):
        raise ValueError("cache object key must be a lowercase SHA-256 identity")


def _cache_key_from_materialization(path: Path) -> str | None:
    name = path.name
    if name.startswith(".") and name.endswith(".tmp"):
        name = name[1:].split(".", maxsplit=1)[0]
    if len(name) not in {64, 128} or any(character not in "0123456789abcdef" for character in name):
        return None
    return name[:64]


class ObjectByteClient(Protocol):
    def put_bytes(
        self,
        key: str,
        data: bytes,
        *,
        bucket: str | None = None,
        content_type: str = "application/octet-stream",
        metadata: dict[str, str] | None = None,
    ) -> S3ObjectInfo: ...

    def put_file(
        self,
        key: str,
        source: str | Path,
        *,
        bucket: str | None = None,
        content_type: str = "application/octet-stream",
        metadata: dict[str, str] | None = None,
    ) -> S3ObjectInfo: ...

    def read_bytes(self, key: str, *, bucket: str | None = None) -> bytes: ...

    def download_file(
        self,
        key: str,
        target: str | Path,
        *,
        bucket: str | None = None,
    ) -> S3ObjectInfo: ...

    def head(self, key: str, *, bucket: str | None = None) -> S3ObjectInfo: ...

    def exists(self, key: str, *, bucket: str | None = None) -> bool: ...

    def generate_presigned_get_url(
        self,
        key: str,
        *,
        bucket: str | None = None,
        expires_seconds: int = 3600,
    ) -> str: ...

    def generate_presigned_put_url(
        self,
        key: str,
        *,
        bucket: str | None = None,
        expires_seconds: int = 3600,
        content_length: int = 0,
        content_type: str = "application/octet-stream",
    ) -> str: ...

    def delete(self, key: str, *, bucket: str | None = None) -> None: ...


class CacheByteClient(Protocol):
    def put_bytes(self, key: str, data: bytes) -> CacheMaterialization: ...

    def read_bytes(self, key: str) -> bytes: ...

    def delete_path(self, path: str | Path) -> None: ...

    def exists_path(self, path: str | Path) -> bool: ...

    def iter_paths(self) -> Iterator[Path]: ...


@dataclass(frozen=True, slots=True)
class CacheMaterialization:
    path: Path
    created: bool


@dataclass(frozen=True, slots=True)
class CacheReconciliationResult:
    records_removed: int = 0
    objects_removed: int = 0


class MountedCacheSettings(BaseSettings):
    root: Path = Field(default_factory=lambda: state_home() / "cache")

    model_config = SettingsConfigDict(env_prefix=f"{ENV_PREFIX}_CACHE_", extra="ignore")

    @field_validator("root")
    @classmethod
    def expand_root(cls, value: Path) -> Path:
        return value.expanduser().resolve()


class MountedCacheClient:
    def __init__(self, settings: MountedCacheSettings | None = None) -> None:
        self.settings = settings or MountedCacheSettings()

    @classmethod
    def from_settings(cls, settings: MountedCacheSettings | None = None) -> MountedCacheClient:
        return cls(settings)

    def path_for_key(self, key: str) -> Path:
        _validate_cache_object_key(key)
        return self.settings.root / key[:2] / key[2:4] / key

    def put_bytes(self, key: str, data: bytes) -> CacheMaterialization:
        target = self.path_for_key(key)
        if len(key) != 128 or key[64:] != _sha256_bytes(data):
            raise ValueError("cache object key does not match the materialized content")
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists() or target.is_symlink():
            self._assert_materialization(target, data)
            return CacheMaterialization(path=target, created=False)
        temp = target.with_name(f".{target.name}.{os.getpid()}.{uuid4().hex}.tmp")
        try:
            with temp.open("xb") as destination:
                destination.write(data)
                destination.flush()
                os.fsync(destination.fileno())
            try:
                os.link(temp, target)
            except FileExistsError:
                self._assert_materialization(target, data)
                return CacheMaterialization(path=target, created=False)
            return CacheMaterialization(path=target, created=True)
        finally:
            temp.unlink(missing_ok=True)

    def read_bytes(self, key: str) -> bytes:
        path = self.path_for_key(key)
        if path.is_symlink():
            raise RuntimeError(f"cache materialization cannot be a symlink: {path}")
        return path.read_bytes()

    def delete_path(self, path: str | Path) -> None:
        owned = self._owned_path(path)
        if owned.is_dir() and not owned.is_symlink():
            raise RuntimeError(f"cache materialization is not a file: {owned}")
        owned.unlink(missing_ok=True)

    def exists_path(self, path: str | Path) -> bool:
        owned = self._owned_path(path)
        return owned.is_file() and not owned.is_symlink()

    def iter_paths(self) -> Iterator[Path]:
        if not self.settings.root.exists():
            return
        for first in sorted(self.settings.root.iterdir()):
            if not first.is_dir() or first.is_symlink():
                continue
            for second in sorted(first.iterdir()):
                if not second.is_dir() or second.is_symlink():
                    continue
                for candidate in sorted(second.iterdir()):
                    if candidate.is_file() or candidate.is_symlink():
                        yield candidate

    def _owned_path(self, path: str | Path) -> Path:
        candidate = Path(path).expanduser()
        if not candidate.is_absolute():
            raise ValueError(f"cache materialization path must be absolute: {candidate}")
        try:
            candidate.relative_to(self.settings.root)
        except ValueError as error:
            raise ValueError(
                f"cache materialization path is outside cache root: {candidate}"
            ) from error
        if candidate == self.settings.root:
            raise ValueError("cache materialization path cannot be the cache root")
        resolved_parent = candidate.parent.resolve()
        if (
            resolved_parent != self.settings.root
            and self.settings.root not in resolved_parent.parents
        ):
            raise ValueError(
                f"cache materialization parent resolves outside cache root: {candidate}"
            )
        return candidate

    @staticmethod
    def _assert_materialization(path: Path, data: bytes) -> None:
        if path.is_symlink() or not path.is_file() or path.read_bytes() != data:
            raise RuntimeError(f"cache materialization conflicts with immutable content: {path}")


class ObjectStorage:
    def __init__(
        self,
        context: StorageContext,
        *,
        object_client: ObjectByteClient | None = None,
        default_bucket: str | None = None,
        allowed_buckets: Collection[str] | None = None,
    ) -> None:
        self.context = context
        self.object_client = object_client or default_s3_object_store_client()
        self.default_bucket = default_bucket or S3ObjectStoreSettings().bucket
        self.allowed_buckets = frozenset(
            {
                WORKSPACE_OBJECT_BUCKET,
                IMAGE_BUILD_CONTEXT_BUCKET,
                SOURCE_PACKAGE_BUCKET,
                self.default_bucket,
                *(allowed_buckets or ()),
            }
        )

    @classmethod
    def from_settings(
        cls,
        context: StorageContext,
        settings: S3ObjectStoreSettings | None = None,
        *,
        allowed_buckets: Collection[str] | None = None,
    ) -> ObjectStorage:
        config = settings or S3ObjectStoreSettings()
        return cls(
            context,
            object_client=S3ObjectStoreClient.from_settings(config),
            default_bucket=config.bucket,
            allowed_buckets=allowed_buckets,
        )

    def put_bytes(
        self,
        bucket: str,
        key: str,
        data: bytes,
        *,
        content_type: str = "application/octet-stream",
        metadata: dict[str, str] | None = None,
    ) -> ObjectRecord:
        return self.put_bytes_for_workspace(
            workspace_id=self._workspace_id(),
            bucket=bucket,
            key=key,
            data=data,
            content_type=content_type,
            metadata=metadata,
        )

    def put_bytes_for_workspace(
        self,
        *,
        workspace_id: str,
        bucket: str,
        key: str,
        data: bytes,
        object_id: str | None = None,
        content_type: str = "application/octet-stream",
        metadata: dict[str, str] | None = None,
    ) -> ObjectRecord:
        self._validate_bucket(bucket)
        physical_bucket = self.physical_bucket(bucket)
        physical_key = self.physical_key_for_workspace(workspace_id, bucket=bucket, key=key)
        path = f"s3://{physical_bucket}/{physical_key}"
        command = ObjectWriteCommand(
            bucket=bucket,
            key=key,
            path=path,
            size=len(data),
            sha256=_sha256_bytes(data),
            content_type=content_type,
            metadata=metadata or {},
        )
        with self.context.database.session() as session:
            claim = ObjectRepository(session).begin_write(
                command,
                workspace_id=workspace_id,
                object_id=object_id,
                overwrite=True,
            )
        try:
            self.object_client.put_bytes(
                physical_key,
                data,
                bucket=physical_bucket,
                content_type=content_type,
                metadata={**(metadata or {}), OBJECT_SHA256_METADATA_KEY: claim.record.sha256},
            )
        except Exception:
            with self.context.database.session() as session:
                ObjectRepository(session).abort_write(claim, workspace_id=workspace_id)
            raise
        with self.context.database.session() as session:
            return ObjectRepository(session).complete_write(claim, workspace_id=workspace_id)

    def put_file(
        self,
        bucket: str,
        key: str,
        source: str | Path,
        *,
        content_type: str = "application/octet-stream",
        metadata: dict[str, str] | None = None,
    ) -> ObjectRecord:
        return self.put_file_for_workspace(
            workspace_id=self._workspace_id(),
            bucket=bucket,
            key=key,
            source=source,
            content_type=content_type,
            metadata=metadata,
            overwrite=True,
        )

    def put_file_for_workspace(
        self,
        *,
        workspace_id: str,
        bucket: str,
        key: str,
        source: str | Path,
        content_type: str = "application/octet-stream",
        metadata: dict[str, str] | None = None,
        overwrite: bool,
    ) -> ObjectRecord:
        self._validate_bucket(bucket)
        source_path = Path(source).expanduser().resolve()
        if not source_path.exists():
            msg = f"object source file not found: {source_path}"
            raise FileNotFoundError(msg)
        if not source_path.is_file():
            msg = f"object source path is not a file: {source_path}"
            raise IsADirectoryError(msg)

        stat = source_path.stat()
        sha256 = _sha256_file(source_path)
        physical_bucket = self.physical_bucket(bucket)
        physical_key = self.physical_key_for_workspace(workspace_id, bucket=bucket, key=key)
        command = ObjectWriteCommand(
            bucket=bucket,
            key=key,
            path=f"s3://{physical_bucket}/{physical_key}",
            size=stat.st_size,
            sha256=sha256,
            content_type=content_type,
            metadata=metadata or {},
        )
        reuse_existing = False
        if not overwrite:
            with self.context.database.session() as session:
                existing = ObjectRepository(session).get_by_bucket_key(
                    bucket,
                    key,
                    workspace_id=workspace_id,
                )
            reuse_existing = existing is not None and self.object_is_complete(existing)
        with self.context.database.session() as session:
            claim = ObjectRepository(session).begin_write(
                command,
                workspace_id=workspace_id,
                overwrite=overwrite,
                reuse_existing=reuse_existing,
            )
        if not claim.write_required:
            return claim.record
        try:
            self.object_client.put_file(
                physical_key,
                source_path,
                bucket=physical_bucket,
                content_type=content_type,
                metadata={**(metadata or {}), OBJECT_SHA256_METADATA_KEY: claim.record.sha256},
            )
        except Exception:
            with self.context.database.session() as session:
                ObjectRepository(session).abort_write(claim, workspace_id=workspace_id)
            raise
        with self.context.database.session() as session:
            return ObjectRepository(session).complete_write(claim, workspace_id=workspace_id)

    def object_is_complete(self, record: ObjectRecord) -> bool:
        """Confirm physical bytes match the durable immutable object identity."""
        physical_key = self.physical_key_for_record(record)
        try:
            physical_bucket = self.physical_bucket(record.bucket)
            if not self.object_client.exists(physical_key, bucket=physical_bucket):
                return False
            info = self.object_client.head(physical_key, bucket=physical_bucket)
        except (KeyError, FileNotFoundError):
            return False
        except Exception as exc:
            raise UpstreamUnavailableError(
                f"object completeness check failed: {record.bucket}/{record.key}"
            ) from exc
        return (
            info.size == record.size
            and info.metadata.get(OBJECT_SHA256_METADATA_KEY) == record.sha256
        )

    def get(self, bucket: str, key: str) -> ObjectRecord:
        return self.get_for_workspace(
            workspace_id=self._workspace_id(),
            bucket=bucket,
            key=key,
        )

    def get_for_workspace(self, *, workspace_id: str, bucket: str, key: str) -> ObjectRecord:
        self._validate_bucket(bucket)
        with self.context.database.session() as session:
            record = ObjectRepository(session).get_by_bucket_key(
                bucket,
                key,
                workspace_id=workspace_id,
            )
        if record is not None:
            return record
        msg = f"object not found: {bucket}/{key}"
        raise NotFoundError(msg)

    def get_by_id(self, object_id: str) -> ObjectRecord:
        return self.get_by_id_for_workspace(
            object_id,
            workspace_id=self._workspace_id(),
        )

    def get_by_id_for_workspace(
        self,
        object_id: str,
        *,
        workspace_id: str,
    ) -> ObjectRecord:
        with self.context.database.session() as session:
            owned = ObjectRepository(session).get_owned(object_id)
            record = (
                owned.record if owned is not None and owned.workspace_id == workspace_id else None
            )
        if record is not None:
            return record
        msg = f"object not found: {object_id}"
        raise NotFoundError(msg)

    def find_by_sha256(self, sha256: str, *, bucket: str | None = None) -> ObjectRecord | None:
        return self.find_by_sha256_for_workspace(
            workspace_id=self._workspace_id(),
            sha256=sha256,
            bucket=bucket,
        )

    def find_by_sha256_for_workspace(
        self,
        *,
        workspace_id: str,
        sha256: str,
        bucket: str | None = None,
    ) -> ObjectRecord | None:
        with self.context.database.session() as session:
            return ObjectRepository(session).find_by_sha256(
                sha256,
                workspace_id=workspace_id,
                bucket=bucket,
            )

    def reserve(
        self,
        bucket: str,
        key: str,
        *,
        size: int,
        sha256: str,
        content_type: str = "application/octet-stream",
        metadata: dict[str, str] | None = None,
        overwrite: bool = False,
    ) -> ObjectRecord:
        return self.reserve_for_workspace(
            workspace_id=self._workspace_id(),
            bucket=bucket,
            key=key,
            size=size,
            sha256=sha256,
            content_type=content_type,
            metadata=metadata,
            overwrite=overwrite,
        )

    def reserve_for_workspace(
        self,
        *,
        workspace_id: str,
        bucket: str,
        key: str,
        size: int,
        sha256: str,
        content_type: str = "application/octet-stream",
        metadata: dict[str, str] | None = None,
        overwrite: bool = False,
    ) -> ObjectRecord:
        self._validate_bucket(bucket)
        physical_bucket = self.physical_bucket(bucket)
        physical_key = self.physical_key_for_workspace(workspace_id, bucket=bucket, key=key)
        command = ObjectWriteCommand(
            bucket=bucket,
            key=key,
            path=f"s3://{physical_bucket}/{physical_key}",
            size=size,
            sha256=sha256,
            content_type=content_type,
            metadata=metadata or {},
        )
        with self.context.database.session() as session:
            return ObjectRepository(session).reserve(
                command,
                workspace_id=workspace_id,
                overwrite=overwrite,
            )

    def read_bytes(self, bucket: str, key: str) -> bytes:
        return self.read_bytes_for_workspace(
            workspace_id=self._workspace_id(),
            bucket=bucket,
            key=key,
        )

    def read_bytes_for_workspace(
        self,
        *,
        workspace_id: str,
        bucket: str,
        key: str,
    ) -> bytes:
        self.get_for_workspace(workspace_id=workspace_id, bucket=bucket, key=key)
        return self.object_client.read_bytes(
            self.physical_key_for_workspace(workspace_id, bucket=bucket, key=key),
            bucket=self.physical_bucket(bucket),
        )

    def read_content_for_workspace(
        self,
        *,
        workspace_id: str,
        bucket: str,
        key: str,
    ) -> tuple[ObjectRecord, bytes]:
        record = self.get_for_workspace(
            workspace_id=workspace_id,
            bucket=bucket,
            key=key,
        )
        return record, self.object_client.read_bytes(
            self.physical_key_for_workspace(workspace_id, bucket=bucket, key=key),
            bucket=self.physical_bucket(bucket),
        )

    def read_by_id(self, object_id: str) -> bytes:
        record = self.get_by_id(object_id)
        return self.object_client.read_bytes(
            self.physical_key_for_record(record),
            bucket=self.physical_bucket(record.bucket),
        )

    def download_file(self, bucket: str, key: str, target: str | Path) -> ObjectRecord:
        return self.download_file_for_workspace(
            workspace_id=self._workspace_id(),
            bucket=bucket,
            key=key,
            target=target,
        )

    def download_file_for_workspace(
        self,
        *,
        workspace_id: str,
        bucket: str,
        key: str,
        target: str | Path,
    ) -> ObjectRecord:
        record = self.get_for_workspace(
            workspace_id=workspace_id,
            bucket=bucket,
            key=key,
        )
        target_path = Path(target).expanduser().resolve()
        target_path.parent.mkdir(parents=True, exist_ok=True)
        self.object_client.download_file(
            self.physical_key_for_workspace(workspace_id, bucket=bucket, key=key),
            target_path,
            bucket=self.physical_bucket(bucket),
        )
        return record

    def download_by_id(self, object_id: str, target: str | Path) -> ObjectRecord:
        return self.download_by_id_for_workspace(
            object_id,
            target,
            workspace_id=self._workspace_id(),
        )

    def download_by_id_for_workspace(
        self,
        object_id: str,
        target: str | Path,
        *,
        workspace_id: str,
    ) -> ObjectRecord:
        record = self.get_by_id_for_workspace(object_id, workspace_id=workspace_id)
        target_path = Path(target).expanduser().resolve()
        target_path.parent.mkdir(parents=True, exist_ok=True)
        self.object_client.download_file(
            self.physical_key_for_workspace(
                workspace_id,
                bucket=record.bucket,
                key=record.key,
            ),
            target_path,
            bucket=self.physical_bucket(record.bucket),
        )
        return record

    def head(self, bucket: str, key: str) -> S3ObjectInfo:
        return self.head_for_workspace(
            workspace_id=self._workspace_id(),
            bucket=bucket,
            key=key,
        )

    def head_for_workspace(
        self,
        *,
        workspace_id: str,
        bucket: str,
        key: str,
    ) -> S3ObjectInfo:
        self.get_for_workspace(workspace_id=workspace_id, bucket=bucket, key=key)
        return self.object_client.head(
            self.physical_key_for_workspace(workspace_id, bucket=bucket, key=key),
            bucket=self.physical_bucket(bucket),
        )

    def generate_presigned_get_url(
        self,
        bucket: str,
        key: str,
        *,
        expires_seconds: int = 3600,
    ) -> str:
        return self.generate_presigned_get_url_for_workspace(
            workspace_id=self._workspace_id(),
            bucket=bucket,
            key=key,
            expires_seconds=expires_seconds,
        )

    def generate_presigned_get_url_for_workspace(
        self,
        *,
        workspace_id: str,
        bucket: str,
        key: str,
        expires_seconds: int = 3600,
    ) -> str:
        self.get_for_workspace(workspace_id=workspace_id, bucket=bucket, key=key)
        return self.object_client.generate_presigned_get_url(
            self.physical_key_for_workspace(workspace_id, bucket=bucket, key=key),
            bucket=self.physical_bucket(bucket),
            expires_seconds=expires_seconds,
        )

    def generate_presigned_put_url_for_workspace(
        self,
        *,
        workspace_id: str,
        bucket: str,
        key: str,
        expires_seconds: int,
        content_length: int,
        content_type: str,
    ) -> str:
        self.get_for_workspace(workspace_id=workspace_id, bucket=bucket, key=key)
        return self.object_client.generate_presigned_put_url(
            self.physical_key_for_workspace(workspace_id, bucket=bucket, key=key),
            bucket=self.physical_bucket(bucket),
            expires_seconds=expires_seconds,
            content_length=content_length,
            content_type=content_type,
        )

    def list(self, bucket: str | None = None, *, prefix: str = "") -> list[ObjectRecord]:
        return self.list_for_workspace(
            workspace_id=self._workspace_id(),
            bucket=bucket,
            prefix=prefix,
        )

    def list_for_workspace(
        self,
        *,
        workspace_id: str,
        bucket: str | None = None,
        prefix: str = "",
    ) -> list[ObjectRecord]:
        if bucket is not None:
            self._validate_bucket(bucket)
        with self.context.database.session() as session:
            records = ObjectRepository(session).list(workspace_id=workspace_id)
        records = [
            record
            for record in records
            if (bucket is None or record.bucket == bucket) and record.key.startswith(prefix)
        ]
        records.sort(key=lambda item: (item.bucket, item.key))
        return records

    def delete(self, bucket: str, key: str) -> None:
        workspace_id = self._workspace_id()
        if not self.delete_for_workspace(
            workspace_id=workspace_id,
            bucket=bucket,
            key=key,
        ):
            msg = f"object not found: {bucket}/{key}"
            raise NotFoundError(msg)

    def delete_for_workspace(
        self,
        *,
        workspace_id: str,
        bucket: str,
        key: str,
    ) -> bool:
        self._validate_bucket(bucket)
        with self.context.database.session() as session:
            repository = ObjectRepository(session)
            record = repository.get_by_bucket_key(
                bucket,
                key,
                workspace_id=workspace_id,
                include_operations=True,
            )
            if record is None:
                return False
            record = repository.claim_delete(
                record.id,
                cleanup_kind=OBJECT_CLEANUP_DELETE,
                claimed_at=utc_now(),
            )
        return self._delete_claimed_object(
            record,
            release_on_confirmation_failure=True,
        )

    def delete_required_for_workspace(
        self,
        *,
        workspace_id: str,
        bucket: str,
        key: str,
    ) -> None:
        if not self.delete_for_workspace(
            workspace_id=workspace_id,
            bucket=bucket,
            key=key,
        ):
            msg = f"object not found: {bucket}/{key}"
            raise NotFoundError(msg)

    def _delete_claimed_object(
        self,
        record: ObjectRecord,
        *,
        release_on_confirmation_failure: bool = False,
        deleting_workspace_id: str | None = None,
    ) -> bool:
        physical_key = self.physical_key_for_record(record)
        physical_bucket = self.physical_bucket(record.bucket)
        self.object_client.delete(physical_key, bucket=physical_bucket)
        if self.object_client.exists(physical_key, bucket=physical_bucket):
            if release_on_confirmation_failure:
                with self.context.database.session() as session:
                    ObjectRepository(session).release_delete_claim(
                        record.id,
                        cleanup_kind=OBJECT_CLEANUP_DELETE,
                    )
            raise UpstreamUnavailableError(
                f"object deletion was not confirmed: {record.bucket}/{record.key}"
            )
        with self.context.database.session() as session:
            repository = ObjectRepository(session)
            owned = repository.get_owned(record.id, include_operations=True)
            if owned is None:
                return False
            if owned.record.cleanup_kind != OBJECT_CLEANUP_DELETE:
                raise RuntimeError(f"object delete claim was replaced: {record.id}")
            if deleting_workspace_id is not None:
                return repository.delete_for_workspace_deletion(
                    record.id,
                    workspace_id=deleting_workspace_id,
                    cleanup_kind=OBJECT_CLEANUP_DELETE,
                )
            return repository.delete_across_workspaces(record.id)

    def reconcile_operations(
        self,
        *,
        now: datetime | None = None,
        lease_seconds: int,
        limit: int,
    ) -> int:
        claimed_before = (now or utc_now()) - timedelta(seconds=lease_seconds)
        with self.context.database.session() as session:
            repository = ObjectRepository(session)
            stale_writes = repository.list_stale_write_claims(
                claimed_before=claimed_before,
                limit=limit,
            )
            stale_deletes = repository.list_stale_delete_claims(
                claimed_before=claimed_before,
                cleanup_kind=OBJECT_CLEANUP_DELETE,
                limit=max(limit - len(stale_writes), 0),
            )
        reconciled = 0
        for owned in stale_writes:
            record = owned.record
            target = record.write_target
            matches_target = False
            physical_key = (
                self.physical_key_for_workspace(
                    owned.workspace_id,
                    bucket=target.bucket,
                    key=target.key,
                )
                if target is not None
                else ""
            )
            if target is not None and self.object_client.exists(
                physical_key,
                bucket=self.physical_bucket(target.bucket),
            ):
                info = self.object_client.head(
                    physical_key,
                    bucket=self.physical_bucket(target.bucket),
                )
                matches_target = (
                    info.size == target.size
                    and info.metadata.get(OBJECT_SHA256_METADATA_KEY) == target.sha256
                )
            claim = ObjectWriteClaim(
                record=(
                    record.model_copy(update=target.model_dump()) if target is not None else record
                ),
                claim_id=record.write_claim_id,
                created=record.write_created,
            )
            with self.context.database.session() as session:
                repository = ObjectRepository(session)
                if matches_target:
                    repository.complete_write(claim, workspace_id=owned.workspace_id)
                else:
                    repository.abort_write(claim, workspace_id=owned.workspace_id)
            reconciled += 1
        for owned in stale_deletes:
            reconciled += int(self._delete_claimed_object(owned.record))
        return reconciled

    def delete_workspace_objects(self, workspace_id: str) -> int:
        records = self.list_for_workspace(workspace_id=workspace_id)
        return sum(
            int(
                self.delete_for_workspace(
                    workspace_id=workspace_id,
                    bucket=record.bucket,
                    key=record.key,
                )
            )
            for record in records
        )

    def delete_workspace_objects_for_deletion(self, workspace_id: str) -> int:
        """Delete every settled object while a workspace is in Deleting state.

        Writes admitted before the workspace deletion fence retain authority to
        finish or abort. Their rows remain durable so finalization can refuse to
        tombstone the workspace until a later deletion attempt cleans them up.
        """
        with self.context.database.session() as session:
            records = ObjectRepository(session).list_for_workspace_deletion(workspace_id)

        deleted = 0
        for record in records:
            if record.write_claimed_at is not None:
                continue
            with self.context.database.session() as session:
                repository = ObjectRepository(session)
                owned = repository.get_owned(record.id, include_operations=True)
                if owned is None:
                    continue
                if owned.workspace_id != workspace_id:
                    raise RuntimeError(f"workspace object ownership changed: {record.id}")
                current = owned.record
                if current.write_claimed_at is not None:
                    continue
                if current.cleanup_claimed_at is not None:
                    if current.cleanup_kind != OBJECT_CLEANUP_DELETE:
                        raise ConflictError(f"object has a different cleanup claim: {current.id}")
                    claimed = current
                else:
                    claimed = repository.claim_delete_for_workspace_deletion(
                        current.id,
                        workspace_id=workspace_id,
                        cleanup_kind=OBJECT_CLEANUP_DELETE,
                        claimed_at=utc_now(),
                    )
            deleted += int(
                self._delete_claimed_object(
                    claimed,
                    release_on_confirmation_failure=False,
                    deleting_workspace_id=workspace_id,
                )
            )
        return deleted

    def _workspace_id(self) -> str:
        with self.context.database.session() as session:
            return self.context.default_workspace_id(session)

    def physical_key_for_record(self, record: ObjectRecord) -> str:
        with self.context.database.session() as session:
            owned = ObjectRepository(session).get_owned(record.id, include_operations=True)
        if owned is None:
            raise NotFoundError(f"object not found: {record.id}")
        return self.physical_key_for_workspace(
            owned.workspace_id,
            bucket=record.bucket,
            key=record.key,
        )

    def physical_key_for_workspace(self, workspace_id: str, *, bucket: str, key: str) -> str:
        self._validate_bucket(bucket)
        if not workspace_id:
            raise InvalidInputError("workspace id is required for object storage")
        return f"workspaces/{workspace_id}/{bucket}/{key}"

    def physical_bucket(self, bucket: str) -> str:
        self._validate_bucket(bucket)
        if bucket in {
            WORKSPACE_OBJECT_BUCKET,
            IMAGE_BUILD_CONTEXT_BUCKET,
            SOURCE_PACKAGE_BUCKET,
            self.default_bucket,
        }:
            return self.default_bucket
        return bucket

    def _validate_bucket(self, bucket: str) -> None:
        if bucket not in self.allowed_buckets:
            raise InvalidInputError(f"object bucket purpose is not allowed: {bucket}")


class CacheStorage:
    def __init__(
        self,
        context: StorageContext,
        *,
        cache_client: CacheByteClient | None = None,
    ) -> None:
        self.context = context
        self.cache_client = cache_client or MountedCacheClient.from_settings()

    def put(
        self,
        namespace: str,
        key: str,
        source: str | Path,
        *,
        expires_at: datetime | None = None,
    ) -> CacheEntry:
        source_path = Path(source).expanduser().resolve()
        return self.put_bytes(
            namespace,
            key,
            source_path.read_bytes(),
            expires_at=expires_at,
        )

    def put_bytes(
        self,
        namespace: str,
        key: str,
        data: bytes,
        *,
        expires_at: datetime | None = None,
    ) -> CacheEntry:
        cache_key = _cache_key(namespace, key)
        sha256 = _sha256_bytes(data)
        materialization: CacheMaterialization | None = None
        previous: CacheEntry | None = None
        try:
            with self.context.database.session() as session:
                repository = CacheEntryRepository(session)
                repository.lock(cache_key)
                previous = repository.get(cache_key)
                materialization = self.cache_client.put_bytes(
                    _cache_object_key(cache_key, sha256),
                    data,
                )
                record = CacheEntry(
                    key=cache_key,
                    path=str(materialization.path),
                    size=len(data),
                    sha256=sha256,
                    expires_at=expires_at,
                )
                stored = repository.upsert(record)
        except Exception as error:
            if materialization is not None and materialization.created:
                try:
                    self._compensate_failed_put(cache_key, materialization.path)
                except Exception as cleanup_error:
                    error.add_note(f"cache put compensation failed: {cleanup_error}")
            raise
        if previous is not None and previous.path != stored.path:
            try:
                self.cache_client.delete_path(previous.path)
            except Exception:
                LOGGER.warning(
                    "cache old-object retirement deferred for cache key %s",
                    stored.key,
                    exc_info=True,
                )
        return stored

    def get(self, namespace: str, key: str) -> CacheEntry:
        cache_key = _cache_key(namespace, key)
        with self.context.database.session() as session:
            repository = CacheEntryRepository(session)
            record = repository.increment_hits(cache_key, updated_at=utc_now())
            if record is None:
                msg = f"cache entry not found: {namespace}/{key}"
                raise NotFoundError(msg)
            return record

    def get_bytes(self, namespace: str, key: str) -> tuple[CacheEntry, bytes]:
        record = self.get(namespace, key)
        return record, self.cache_client.read_bytes(Path(record.path).name)

    def list(self) -> list[CacheEntry]:
        with self.context.database.session() as session:
            records = CacheEntryRepository(session).list()
        records.sort(key=lambda item: item.updated_at, reverse=True)
        return records

    def delete(self, namespace: str, key: str) -> None:
        cache_key = _cache_key(namespace, key)
        if not self.delete_key(cache_key):
            msg = f"cache entry not found: {namespace}/{key}"
            raise NotFoundError(msg)

    def delete_key(self, cache_key: str) -> bool:
        with self.context.database.session() as session:
            repository = CacheEntryRepository(session)
            repository.lock(cache_key)
            record = repository.get(cache_key)
            if record is None:
                return False
            self.cache_client.delete_path(record.path)
            deleted = repository.delete(cache_key)
        return deleted

    def reconcile(self, *, limit: int) -> CacheReconciliationResult:
        if limit <= 0:
            return CacheReconciliationResult()
        with self.context.database.session() as session:
            records = CacheEntryRepository(session).list()
        records_removed = 0
        for record in records:
            if records_removed == limit:
                break
            records_removed += int(self._delete_missing_record(record))
        objects_removed = 0
        for path in self.cache_client.iter_paths():
            if objects_removed == limit:
                break
            objects_removed += int(self._delete_unreferenced_path(path))
        return CacheReconciliationResult(
            records_removed=records_removed,
            objects_removed=objects_removed,
        )

    def _compensate_failed_put(self, cache_key: str, path: Path) -> None:
        with self.context.database.session() as session:
            repository = CacheEntryRepository(session)
            repository.lock(cache_key)
            current = repository.get(cache_key)
            if current is None or current.path != str(path):
                self.cache_client.delete_path(path)

    def _delete_missing_record(self, candidate: CacheEntry) -> bool:
        if self.cache_client.exists_path(candidate.path):
            return False
        with self.context.database.session() as session:
            repository = CacheEntryRepository(session)
            repository.lock(candidate.key)
            current = repository.get(candidate.key)
            if (
                current is None
                or current.path != candidate.path
                or self.cache_client.exists_path(current.path)
            ):
                return False
            self.cache_client.delete_path(current.path)
            return repository.delete(current.key)

    def _delete_unreferenced_path(self, path: Path) -> bool:
        cache_key = _cache_key_from_materialization(path)
        with self.context.database.session() as session:
            repository = CacheEntryRepository(session)
            if cache_key is not None:
                repository.lock(cache_key)
            if repository.path_is_referenced(str(path)):
                return False
            existed = path.exists() or path.is_symlink()
            self.cache_client.delete_path(path)
            return existed
