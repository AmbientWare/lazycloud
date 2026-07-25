from __future__ import annotations

import base64
import glob
import shutil
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from tempfile import TemporaryDirectory
from typing import Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from uuid import uuid4

from pydantic_settings import BaseSettings, SettingsConfigDict
from shared.app_identity import ENV_PREFIX
from shared.errors import InvalidInputError, NotFoundError, UpstreamUnavailableError
from shared.http.volumes import PresignedUrlMethod
from storage_client.s3 import S3ObjectInfo, S3ObjectStoreClient, S3ObjectStoreSettings

from storage.http import request_storage_http

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


class JuiceFsGatewaySettings(BaseSettings):
    endpoint_url: str = "http://juicefs-gateway:9900"
    webdav_endpoint_url: str = "http://juicefs-webdav:9901"
    presigned_endpoint_url: str | None = None
    bucket: str = "lazycloud"
    region_name: str = "us-east-1"
    access_key_id: str = ""
    secret_access_key: str = ""
    force_path_style: bool = True
    transfer_multipart_threshold_bytes: int = 64 * 1024 * 1024
    transfer_multipart_chunk_size_bytes: int = 64 * 1024 * 1024
    transfer_max_concurrency: int = 2

    model_config = SettingsConfigDict(
        env_prefix=f"{ENV_PREFIX}_JUICEFS_GATEWAY_",
        env_file=".env",
        extra="ignore",
    )

    def s3_settings(self) -> S3ObjectStoreSettings:
        return S3ObjectStoreSettings(
            bucket=self.bucket,
            endpoint_url=self.endpoint_url,
            presigned_endpoint_url=self.presigned_endpoint_url,
            region_name=self.region_name,
            access_key_id=self.access_key_id,
            secret_access_key=self.secret_access_key,
            force_path_style=self.force_path_style,
            transfer_multipart_threshold_bytes=self.transfer_multipart_threshold_bytes,
            transfer_multipart_chunk_size_bytes=self.transfer_multipart_chunk_size_bytes,
            transfer_max_concurrency=self.transfer_max_concurrency,
        )


class VolumeGatewayClient(Protocol):
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


class VolumeRenameClient(Protocol):
    def move_path(self, source_key: str, destination_key: str) -> None: ...


@dataclass(frozen=True, slots=True)
class JuiceFsWebDavClient:
    endpoint_url: str
    username: str
    password: str
    timeout_seconds: float = 30.0

    def move_path(self, source_key: str, destination_key: str) -> None:
        source_url = self._url(source_key)
        destination_url = self._url(destination_key)
        headers = {
            "Destination": destination_url,
            "Overwrite": "F",
        }
        if self.username or self.password:
            credentials = base64.b64encode(f"{self.username}:{self.password}".encode()).decode(
                "ascii"
            )
            headers["Authorization"] = f"Basic {credentials}"
        try:
            response = request_storage_http(
                source_url,
                method="MOVE",
                headers=headers,
                timeout_seconds=self.timeout_seconds,
            )
            if response.status not in {201, 204}:
                raise UpstreamUnavailableError("JuiceFS rename returned an invalid response")
        except HTTPError as exc:
            if exc.code == 404:
                raise NotFoundError(f"error finding original path {source_key}") from exc
            if exc.code in {409, 412}:
                raise InvalidInputError("destination path already exists or is invalid") from exc
            raise UpstreamUnavailableError("JuiceFS rename is unavailable") from exc
        except (OSError, URLError) as exc:
            raise UpstreamUnavailableError("JuiceFS rename is unavailable") from exc

    def _url(self, key: str) -> str:
        return f"{self.endpoint_url.rstrip('/')}/{quote(key, safe='/')}"


@dataclass(slots=True)
class JuiceFsGatewayVolumeFilesystem:
    client: VolumeGatewayClient
    rename_client: VolumeRenameClient
    bucket: str = "lazycloud"

    @classmethod
    def from_settings(
        cls,
        settings: JuiceFsGatewaySettings | None = None,
    ) -> JuiceFsGatewayVolumeFilesystem:
        config = settings or JuiceFsGatewaySettings()
        return cls(
            client=S3ObjectStoreClient.from_settings(config.s3_settings()),
            rename_client=JuiceFsWebDavClient(
                endpoint_url=config.webdav_endpoint_url,
                username=config.access_key_id,
                password=config.secret_access_key,
            ),
            bucket=config.bucket,
        )

    def ensure_volume(self, namespace: VolumeNamespace) -> None:
        _validate_namespace(namespace)

    def delete_volume(self, namespace: VolumeNamespace) -> None:
        self.client.delete_prefix(_volume_prefix(namespace), bucket=self.bucket)

    def write_path(
        self,
        namespace: VolumeNamespace,
        relative_path: str,
        chunks: Iterable[bytes],
    ) -> None:
        key = _volume_key(namespace, relative_path, require_file=True)
        with TemporaryDirectory(prefix="lazycloud-volume-upload-") as temporary_directory:
            staged = Path(temporary_directory) / "payload"
            with staged.open("wb") as handle:
                for chunk in chunks:
                    handle.write(chunk)
            self.client.put_file(key, staged, bucket=self.bucket)

    def list_path(
        self,
        namespace: VolumeNamespace,
        relative_path: str,
    ) -> tuple[VolumeFilesystemEntry, ...]:
        key = _volume_key(namespace, relative_path)
        root_key = _volume_root_key(namespace)
        return tuple(
            _gateway_entry(item, root_key)
            for item in self.client.list_directory(key, bucket=self.bucket)
        )

    def stat_path(
        self,
        namespace: VolumeNamespace,
        relative_path: str,
    ) -> VolumeFilesystemEntry:
        key = _volume_key(namespace, relative_path)
        root_key = _volume_root_key(namespace)
        if self.client.exists(key, bucket=self.bucket):
            return _gateway_entry(self.client.head(key, bucket=self.bucket), root_key)
        children = self.client.list_directory(key, bucket=self.bucket)
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
        key = _volume_key(namespace, relative_path, require_file=True)
        root_key = _volume_root_key(namespace)
        deleted: list[str] = []
        if self.client.exists(key, bucket=self.bucket):
            self.client.delete(key, bucket=self.bucket)
            deleted.append(key)
        deleted.extend(self.client.delete_prefix(f"{key}/", bucket=self.bucket))
        return tuple(_relative_key(deleted_key, root_key) for deleted_key in deleted)

    def move_path(
        self,
        namespace: VolumeNamespace,
        source_path: str,
        destination_path: str,
    ) -> None:
        source_key = _volume_key(namespace, source_path, require_file=True)
        destination_key = _volume_key(namespace, destination_path, require_file=True)
        if destination_key.startswith(f"{source_key}/"):
            raise InvalidInputError("a directory cannot be moved inside itself")
        self.rename_client.move_path(source_key, destination_key)

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
        key = _volume_key(namespace, relative_path, require_file=True)
        if method is PresignedUrlMethod.GetObject:
            return self.client.generate_presigned_get_url(
                key,
                bucket=self.bucket,
                expires_seconds=expires_seconds,
            )
        if method is PresignedUrlMethod.HeadObject:
            return self.client.generate_presigned_head_url(
                key,
                bucket=self.bucket,
                expires_seconds=expires_seconds,
            )
        if method is PresignedUrlMethod.PutObject:
            return self.client.generate_presigned_put_url(
                key,
                bucket=self.bucket,
                expires_seconds=expires_seconds,
                content_length=content_length,
                content_type=content_type,
            )
        if not upload_id or part_number <= 0:
            raise InvalidInputError("multipart upload ID and positive part number are required")
        return self.client.generate_presigned_upload_part_url(
            key,
            upload_id=upload_id,
            part_number=part_number,
            bucket=self.bucket,
            expires_seconds=expires_seconds,
        )

    def create_multipart_upload(
        self,
        namespace: VolumeNamespace,
        relative_path: str,
    ) -> str:
        return self.client.create_multipart_upload(
            _volume_key(namespace, relative_path, require_file=True),
            bucket=self.bucket,
        )

    def complete_multipart_upload(
        self,
        namespace: VolumeNamespace,
        relative_path: str,
        *,
        upload_id: str,
        completed_parts: tuple[tuple[int, str], ...],
    ) -> None:
        self.client.complete_multipart_upload(
            _volume_key(namespace, relative_path, require_file=True),
            upload_id=upload_id,
            completed_parts=completed_parts,
            bucket=self.bucket,
        )

    def abort_multipart_upload(
        self,
        namespace: VolumeNamespace,
        relative_path: str,
        *,
        upload_id: str,
    ) -> None:
        self.client.abort_multipart_upload(
            _volume_key(namespace, relative_path, require_file=True),
            upload_id=upload_id,
            bucket=self.bucket,
        )

    def occupancy_bytes(self, namespace: VolumeNamespace) -> int:
        return sum(
            item.size or 0
            for item in self.client.list_prefix(
                _volume_prefix(namespace),
                bucket=self.bucket,
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
    _validate_namespace(namespace)
    return f"{VOLUME_NAMESPACE_PREFIX}/{namespace.workspace_id}/{namespace.volume_id}"


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


def _gateway_entry(info: S3ObjectInfo, root_key: str) -> VolumeFilesystemEntry:
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
    "JuiceFsGatewaySettings",
    "JuiceFsGatewayVolumeFilesystem",
    "JuiceFsWebDavClient",
    "LocalVolumeFilesystem",
    "VolumeFilesystem",
    "VolumeFilesystemEntry",
    "VolumeNamespace",
    "VolumeRenameClient",
]
