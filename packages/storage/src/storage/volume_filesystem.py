from __future__ import annotations

import glob
import shutil
import threading
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from tempfile import TemporaryDirectory
from typing import Protocol
from uuid import uuid4

from shared.errors import InvalidInputError, NotFoundError, UpstreamUnavailableError
from shared.http.volumes import PresignedUrlMethod
from shared.identity import WorkspaceStorageConfig
from storage_client.s3 import S3ObjectInfo, S3ObjectStoreClient, S3ObjectStoreSettings

VOLUME_NAMESPACE_PREFIX = "volumes"


@dataclass(frozen=True, slots=True)
class VolumeNamespace:
    workspace_id: str
    volume_id: str


@dataclass(frozen=True, slots=True)
class VolumeFilesystemEntry:
    path: str
    size: int
    modified_at: datetime
    is_dir: bool


class VolumeFilesystem(Protocol):
    def ensure_volume(self, namespace: VolumeNamespace) -> None: ...

    def delete_volume(self, namespace: VolumeNamespace) -> None: ...

    def write_path(
        self,
        namespace: VolumeNamespace,
        relative_path: str,
        chunks: Iterable[bytes],
    ) -> None: ...

    def list_path(
        self,
        namespace: VolumeNamespace,
        relative_path: str,
    ) -> tuple[VolumeFilesystemEntry, ...]: ...

    def stat_path(
        self,
        namespace: VolumeNamespace,
        relative_path: str,
    ) -> VolumeFilesystemEntry: ...

    def delete_path(
        self,
        namespace: VolumeNamespace,
        relative_path: str,
    ) -> tuple[str, ...]: ...

    def move_path(
        self,
        namespace: VolumeNamespace,
        source_path: str,
        destination_path: str,
    ) -> None: ...

    def create_presigned_url(
        self,
        namespace: VolumeNamespace,
        relative_path: str,
        *,
        method: PresignedUrlMethod,
        expires_seconds: int,
        upload_id: str = "",
        part_number: int = 0,
        content_length: int = 0,
        content_type: str = "application/octet-stream",
    ) -> str: ...

    def create_multipart_upload(
        self,
        namespace: VolumeNamespace,
        relative_path: str,
    ) -> str: ...

    def complete_multipart_upload(
        self,
        namespace: VolumeNamespace,
        relative_path: str,
        *,
        upload_id: str,
        completed_parts: tuple[tuple[int, str], ...],
    ) -> None: ...

    def abort_multipart_upload(
        self,
        namespace: VolumeNamespace,
        relative_path: str,
        *,
        upload_id: str,
    ) -> None: ...

    def occupancy_bytes(self, namespace: VolumeNamespace) -> int: ...


class VolumeObjectClient(Protocol):
    def put_file(
        self,
        key: str,
        source: str | Path,
        *,
        bucket: str | None = None,
        content_type: str = "application/octet-stream",
        metadata: dict[str, str] | None = None,
    ) -> S3ObjectInfo: ...

    def exists(self, key: str, *, bucket: str | None = None) -> bool: ...

    def head(self, key: str, *, bucket: str | None = None) -> S3ObjectInfo: ...

    def list_directory(
        self,
        prefix: str,
        *,
        bucket: str | None = None,
    ) -> tuple[S3ObjectInfo, ...]: ...

    def list_prefix(
        self,
        prefix: str,
        *,
        bucket: str | None = None,
    ) -> tuple[S3ObjectInfo, ...]: ...

    def delete_prefix(self, prefix: str, *, bucket: str | None = None) -> tuple[str, ...]: ...

    def delete(self, key: str, *, bucket: str | None = None) -> None: ...

    def copy(
        self,
        source_key: str,
        destination_key: str,
        *,
        bucket: str | None = None,
        source_bucket: str | None = None,
    ) -> None: ...

    def generate_presigned_get_url(
        self,
        key: str,
        *,
        bucket: str | None = None,
        expires_seconds: int = 3600,
    ) -> str: ...

    def generate_presigned_head_url(
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

    def create_multipart_upload(self, key: str, *, bucket: str | None = None) -> str: ...

    def generate_presigned_upload_part_url(
        self,
        key: str,
        *,
        upload_id: str,
        part_number: int,
        bucket: str | None = None,
        expires_seconds: int = 3600,
    ) -> str: ...

    def complete_multipart_upload(
        self,
        key: str,
        *,
        upload_id: str,
        completed_parts: tuple[tuple[int, str], ...],
        bucket: str | None = None,
    ) -> None: ...

    def abort_multipart_upload(
        self,
        key: str,
        *,
        upload_id: str,
        bucket: str | None = None,
    ) -> None: ...


@dataclass(frozen=True, slots=True)
class WorkspaceVolumeStore:
    """The workspace's own object store: volumes live inside the workspace bucket."""

    client: VolumeObjectClient
    bucket: str


class WorkspaceVolumeStoreResolver(Protocol):
    def __call__(self, workspace_id: str) -> WorkspaceVolumeStore: ...


def workspace_volume_store(
    storage: WorkspaceStorageConfig,
    *,
    presigned_endpoint_url: str | None = None,
) -> WorkspaceVolumeStore:
    bucket = storage.bucket or ""
    if not bucket:
        raise UpstreamUnavailableError("workspace storage is not configured for this workspace")
    return WorkspaceVolumeStore(
        client=S3ObjectStoreClient.from_settings(
            S3ObjectStoreSettings(
                bucket=bucket,
                endpoint_url=storage.endpoint_url or None,
                presigned_endpoint_url=presigned_endpoint_url,
                region_name=storage.region or "us-east-1",
                access_key_id=storage.access_key,
                secret_access_key=storage.secret_key,
                force_path_style=storage.force_path_style,
            )
        ),
        bucket=bucket,
    )


def workspace_presign_endpoint(
    storage: WorkspaceStorageConfig,
    *,
    default_endpoint_url: str | None,
    default_presigned_endpoint_url: str | None,
) -> str | None:
    """Presign a workspace bucket held on platform storage through the reachable host.

    The stored endpoint is the one workers use from inside the deployment; a
    client resolving that name would fail. Storage attached by the customer keeps
    its own endpoint, which is already reachable.
    """
    if not default_presigned_endpoint_url:
        return None
    if _same_endpoint(storage.endpoint_url, default_endpoint_url or ""):
        return default_presigned_endpoint_url
    return None


def _same_endpoint(left: str, right: str) -> bool:
    return left.strip().rstrip("/") == right.strip().rstrip("/")


@dataclass(slots=True)
class WorkspaceVolumeFilesystem:
    """Address every volume inside the bucket owned by its own workspace.

    A container's bind and a client's presigned URL therefore reach the same
    object, and credentials never span workspaces.
    """

    resolve_store: WorkspaceVolumeStoreResolver
    _stores: dict[str, WorkspaceVolumeStore] = field(default_factory=dict)
    _stores_lock: threading.Lock = field(default_factory=threading.Lock)

    def ensure_volume(self, namespace: VolumeNamespace) -> None:
        self._store(namespace)

    def delete_volume(self, namespace: VolumeNamespace) -> None:
        store = self._store(namespace)
        store.client.delete_prefix(_volume_prefix(namespace), bucket=store.bucket)

    def _store(self, namespace: VolumeNamespace) -> WorkspaceVolumeStore:
        _validate_namespace(namespace)
        workspace_id = namespace.workspace_id
        with self._stores_lock:
            cached = self._stores.get(workspace_id)
        if cached is not None:
            return cached
        resolved = self.resolve_store(workspace_id)
        with self._stores_lock:
            return self._stores.setdefault(workspace_id, resolved)

    def write_path(
        self,
        namespace: VolumeNamespace,
        relative_path: str,
        chunks: Iterable[bytes],
    ) -> None:
        store = self._store(namespace)
        key = _volume_key(namespace, relative_path, require_file=True)
        with TemporaryDirectory(prefix="lazycloud-volume-upload-") as temporary_directory:
            staged = Path(temporary_directory) / "payload"
            with staged.open("wb") as handle:
                for chunk in chunks:
                    handle.write(chunk)
            store.client.put_file(key, staged, bucket=store.bucket)

    def list_path(
        self,
        namespace: VolumeNamespace,
        relative_path: str,
    ) -> tuple[VolumeFilesystemEntry, ...]:
        store = self._store(namespace)
        key = _volume_key(namespace, relative_path)
        root_key = _volume_root_key(namespace)
        return tuple(
            _object_entry(item, root_key)
            for item in store.client.list_directory(key, bucket=store.bucket)
        )

    def stat_path(
        self,
        namespace: VolumeNamespace,
        relative_path: str,
    ) -> VolumeFilesystemEntry:
        store = self._store(namespace)
        key = _volume_key(namespace, relative_path)
        root_key = _volume_root_key(namespace)
        if store.client.exists(key, bucket=store.bucket):
            return _object_entry(store.client.head(key, bucket=store.bucket), root_key)
        children = store.client.list_directory(key, bucket=store.bucket)
        if not children:
            raise NotFoundError("Path does not exist")
        return VolumeFilesystemEntry(
            path=_relative_key(key, root_key),
            size=0,
            modified_at=max(
                (item.last_modified or datetime.fromtimestamp(0, UTC) for item in children),
                default=datetime.fromtimestamp(0, UTC),
            ),
            is_dir=True,
        )

    def delete_path(
        self,
        namespace: VolumeNamespace,
        relative_path: str,
    ) -> tuple[str, ...]:
        store = self._store(namespace)
        key = _volume_key(namespace, relative_path, require_file=True)
        root_key = _volume_root_key(namespace)
        deleted: list[str] = []
        if store.client.exists(key, bucket=store.bucket):
            store.client.delete(key, bucket=store.bucket)
            deleted.append(key)
        deleted.extend(store.client.delete_prefix(f"{key}/", bucket=store.bucket))
        return tuple(_relative_key(deleted_key, root_key) for deleted_key in deleted)

    def move_path(
        self,
        namespace: VolumeNamespace,
        source_path: str,
        destination_path: str,
    ) -> None:
        """Rename through server-side copy then delete.

        Object storage has no rename, so this is not atomic: a failure between
        the copies and the deletes leaves the source in place, which loses no
        data. Reads of the destination stay 404 until every copy lands.
        """
        store = self._store(namespace)
        source_key = _volume_key(namespace, source_path, require_file=True)
        destination_key = _volume_key(namespace, destination_path, require_file=True)
        if destination_key.startswith(f"{source_key}/"):
            raise InvalidInputError("a directory cannot be moved inside itself")
        moves: list[tuple[str, str]] = []
        if store.client.exists(source_key, bucket=store.bucket):
            moves.append((source_key, destination_key))
        moves.extend(
            (item.key, destination_key + item.key.removeprefix(source_key))
            for item in store.client.list_prefix(f"{source_key}/", bucket=store.bucket)
        )
        if not moves:
            raise NotFoundError(f"error finding original path {source_path}")
        if any(store.client.exists(target, bucket=store.bucket) for _, target in moves):
            raise InvalidInputError("destination path already exists or is invalid")
        for source, target in moves:
            store.client.copy(source, target, bucket=store.bucket)
        for source, _ in moves:
            store.client.delete(source, bucket=store.bucket)

    def create_presigned_url(
        self,
        namespace: VolumeNamespace,
        relative_path: str,
        *,
        method: PresignedUrlMethod,
        expires_seconds: int,
        upload_id: str = "",
        part_number: int = 0,
        content_length: int = 0,
        content_type: str = "application/octet-stream",
    ) -> str:
        store = self._store(namespace)
        key = _volume_key(namespace, relative_path, require_file=True)
        if method is PresignedUrlMethod.GetObject:
            return store.client.generate_presigned_get_url(
                key,
                bucket=store.bucket,
                expires_seconds=expires_seconds,
            )
        if method is PresignedUrlMethod.HeadObject:
            return store.client.generate_presigned_head_url(
                key,
                bucket=store.bucket,
                expires_seconds=expires_seconds,
            )
        if method is PresignedUrlMethod.PutObject:
            return store.client.generate_presigned_put_url(
                key,
                bucket=store.bucket,
                expires_seconds=expires_seconds,
                content_length=content_length,
                content_type=content_type,
            )
        if not upload_id or part_number <= 0:
            raise InvalidInputError("multipart upload ID and positive part number are required")
        return store.client.generate_presigned_upload_part_url(
            key,
            upload_id=upload_id,
            part_number=part_number,
            bucket=store.bucket,
            expires_seconds=expires_seconds,
        )

    def create_multipart_upload(
        self,
        namespace: VolumeNamespace,
        relative_path: str,
    ) -> str:
        store = self._store(namespace)
        return store.client.create_multipart_upload(
            _volume_key(namespace, relative_path, require_file=True),
            bucket=store.bucket,
        )

    def complete_multipart_upload(
        self,
        namespace: VolumeNamespace,
        relative_path: str,
        *,
        upload_id: str,
        completed_parts: tuple[tuple[int, str], ...],
    ) -> None:
        store = self._store(namespace)
        store.client.complete_multipart_upload(
            _volume_key(namespace, relative_path, require_file=True),
            upload_id=upload_id,
            completed_parts=completed_parts,
            bucket=store.bucket,
        )

    def abort_multipart_upload(
        self,
        namespace: VolumeNamespace,
        relative_path: str,
        *,
        upload_id: str,
    ) -> None:
        store = self._store(namespace)
        store.client.abort_multipart_upload(
            _volume_key(namespace, relative_path, require_file=True),
            upload_id=upload_id,
            bucket=store.bucket,
        )

    def occupancy_bytes(self, namespace: VolumeNamespace) -> int:
        store = self._store(namespace)
        return sum(
            item.size or 0
            for item in store.client.list_prefix(
                _volume_prefix(namespace),
                bucket=store.bucket,
            )
        )


@dataclass(slots=True)
class LocalVolumeFilesystem:
    root: Path

    def ensure_volume(self, namespace: VolumeNamespace) -> None:
        self._volume_root(namespace).mkdir(parents=True, exist_ok=True)

    def delete_volume(self, namespace: VolumeNamespace) -> None:
        shutil.rmtree(self._volume_root(namespace), ignore_errors=True)

    def write_path(
        self,
        namespace: VolumeNamespace,
        relative_path: str,
        chunks: Iterable[bytes],
    ) -> None:
        target = self._path(namespace, relative_path, require_file=True)
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(f".{target.name}.{uuid4().hex}.tmp")
        try:
            with temporary.open("wb") as handle:
                for chunk in chunks:
                    handle.write(chunk)
            temporary.replace(target)
        finally:
            temporary.unlink(missing_ok=True)

    def list_path(
        self,
        namespace: VolumeNamespace,
        relative_path: str,
    ) -> tuple[VolumeFilesystemEntry, ...]:
        root = self._volume_root(namespace)
        target = self._path(namespace, relative_path)
        if target.exists() and target.is_dir():
            matches = tuple(sorted(target.iterdir()))
        else:
            matches = tuple(Path(path).resolve() for path in glob.glob(str(target)))
        return tuple(_local_entry(path, root) for path in matches)

    def stat_path(
        self,
        namespace: VolumeNamespace,
        relative_path: str,
    ) -> VolumeFilesystemEntry:
        root = self._volume_root(namespace)
        target = self._path(namespace, relative_path)
        if not target.exists():
            raise NotFoundError("Path does not exist")
        return _local_entry(target, root)

    def delete_path(
        self,
        namespace: VolumeNamespace,
        relative_path: str,
    ) -> tuple[str, ...]:
        root = self._volume_root(namespace)
        target = self._path(namespace, relative_path, require_file=True)
        matches = tuple(Path(path).resolve() for path in glob.glob(str(target)))
        deleted: list[str] = []
        for selected in matches:
            if root not in selected.parents:
                raise InvalidInputError("parent directory cannot be deleted")
            deleted.append(selected.relative_to(root).as_posix())
            if selected.is_dir():
                shutil.rmtree(selected)
            else:
                selected.unlink(missing_ok=True)
        return tuple(deleted)

    def move_path(
        self,
        namespace: VolumeNamespace,
        source_path: str,
        destination_path: str,
    ) -> None:
        source = self._path(namespace, source_path, require_file=True)
        destination = self._path(namespace, destination_path, require_file=True)
        if not source.exists():
            raise NotFoundError(f"error finding original path {source_path}")
        destination.parent.mkdir(parents=True, exist_ok=True)
        source.replace(destination)

    def create_presigned_url(
        self,
        namespace: VolumeNamespace,
        relative_path: str,
        *,
        method: PresignedUrlMethod,
        expires_seconds: int,
        upload_id: str = "",
        part_number: int = 0,
        content_length: int = 0,
        content_type: str = "application/octet-stream",
    ) -> str:
        del expires_seconds, upload_id, part_number, content_length, content_type
        if method is not PresignedUrlMethod.GetObject:
            raise UpstreamUnavailableError("signed writes require the configured JuiceFS gateway")
        target = self._path(namespace, relative_path, require_file=True)
        if not target.is_file():
            raise NotFoundError("Path does not exist")
        return target.as_uri()

    def create_multipart_upload(
        self,
        namespace: VolumeNamespace,
        relative_path: str,
    ) -> str:
        del namespace, relative_path
        raise UpstreamUnavailableError("multipart uploads require the configured JuiceFS gateway")

    def complete_multipart_upload(
        self,
        namespace: VolumeNamespace,
        relative_path: str,
        *,
        upload_id: str,
        completed_parts: tuple[tuple[int, str], ...],
    ) -> None:
        del namespace, relative_path, upload_id, completed_parts
        raise UpstreamUnavailableError("multipart uploads require the configured JuiceFS gateway")

    def abort_multipart_upload(
        self,
        namespace: VolumeNamespace,
        relative_path: str,
        *,
        upload_id: str,
    ) -> None:
        del namespace, relative_path, upload_id
        raise UpstreamUnavailableError("multipart uploads require the configured JuiceFS gateway")

    def occupancy_bytes(self, namespace: VolumeNamespace) -> int:
        root = self._volume_root(namespace)
        if not root.exists():
            return 0
        return sum(path.stat().st_size for path in root.rglob("*") if path.is_file())

    def resolve_path(self, namespace: VolumeNamespace, relative_path: str = ".") -> Path:
        return self._path(namespace, relative_path)

    def _volume_root(self, namespace: VolumeNamespace) -> Path:
        _validate_namespace(namespace)
        root = self.root.expanduser().resolve()
        return (root / namespace.workspace_id / namespace.volume_id).resolve()

    def _path(
        self,
        namespace: VolumeNamespace,
        relative_path: str,
        *,
        require_file: bool = False,
    ) -> Path:
        root = self._volume_root(namespace)
        relative = _normalize_relative_path(relative_path, require_file=require_file)
        target = (root / relative).resolve()
        if target != root and root not in target.parents:
            raise InvalidInputError("parent directory does not exist")
        return target


def _validate_namespace(namespace: VolumeNamespace) -> None:
    for field_name, value in (
        ("workspace ID", namespace.workspace_id),
        ("volume ID", namespace.volume_id),
    ):
        path = PurePosixPath(value)
        if not value or path.is_absolute() or len(path.parts) != 1 or path.parts[0] in {".", ".."}:
            raise InvalidInputError(f"invalid {field_name}")


def _normalize_relative_path(relative_path: str, *, require_file: bool = False) -> str:
    normalized = PurePosixPath(relative_path or ".")
    if normalized.is_absolute() or ".." in normalized.parts:
        raise InvalidInputError("parent directory does not exist")
    value = normalized.as_posix()
    if require_file and value in {"", "."}:
        raise InvalidInputError("parent directory cannot be modified")
    return value


def _volume_root_key(namespace: VolumeNamespace) -> str:
    # The bucket already belongs to one workspace, so the key carries no
    # workspace segment. This is the identity the design rests on: it is exactly
    # the path the worker's mount exposes inside the workspace storage mount.
    _validate_namespace(namespace)
    return f"{VOLUME_NAMESPACE_PREFIX}/{namespace.volume_id}"


def _volume_prefix(namespace: VolumeNamespace) -> str:
    return f"{_volume_root_key(namespace)}/"


def _volume_key(
    namespace: VolumeNamespace,
    relative_path: str,
    *,
    require_file: bool = False,
) -> str:
    root = _volume_root_key(namespace)
    relative = _normalize_relative_path(relative_path, require_file=require_file)
    return root if relative == "." else f"{root}/{relative}"


def _relative_key(key: str, root_key: str) -> str:
    clean = key.rstrip("/")
    if clean == root_key:
        return "."
    prefix = f"{root_key}/"
    if not clean.startswith(prefix):
        raise InvalidInputError("volume gateway returned a path outside the volume namespace")
    return clean.removeprefix(prefix)


def _object_entry(info: S3ObjectInfo, root_key: str) -> VolumeFilesystemEntry:
    return VolumeFilesystemEntry(
        path=_relative_key(info.key, root_key),
        size=info.size or 0,
        modified_at=info.last_modified or datetime.fromtimestamp(0, UTC),
        is_dir=info.key.endswith("/"),
    )


def _local_entry(path: Path, root: Path) -> VolumeFilesystemEntry:
    stat = path.stat()
    return VolumeFilesystemEntry(
        path=path.relative_to(root).as_posix(),
        size=stat.st_size,
        modified_at=datetime.fromtimestamp(stat.st_mtime, UTC),
        is_dir=path.is_dir(),
    )


__all__ = [
    "VOLUME_NAMESPACE_PREFIX",
    "LocalVolumeFilesystem",
    "VolumeFilesystem",
    "VolumeFilesystemEntry",
    "VolumeNamespace",
    "VolumeObjectClient",
    "WorkspaceVolumeFilesystem",
    "WorkspaceVolumeStore",
    "WorkspaceVolumeStoreResolver",
    "workspace_presign_endpoint",
    "workspace_volume_store",
]
