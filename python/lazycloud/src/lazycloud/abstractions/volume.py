from __future__ import annotations

import math
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from pydantic import (
    AliasChoices,
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    field_validator,
    model_validator,
)

from lazycloud._shared.deployment_records import VolumeMount
from lazycloud._shared.enums import StringEnum
from lazycloud._shared.mounts import MountAuthMode, infer_mount_auth_mode, normalize_mount_prefix
from lazycloud.control import ResourceControlBinding, storage_client

# The storage client and the API models load on first use, not when an app
# declares a volume.
if TYPE_CHECKING:
    from lazycloud.clients.storage import StorageClient
    from lazycloud.contracts import api

DEFAULT_VOLUME_MOUNT_ROOT = "/volumes"
DEFAULT_MULTIPART_CHUNK_SIZE_BYTES = 5 * 1024 * 1024
DEFAULT_VOLUME_DOWNLOAD_TIMEOUT_SECONDS = 30.0
# put() sends files above this size as a multipart upload.
MULTIPART_THRESHOLD_BYTES = 64 * 1024 * 1024
_PUT_PART_SIZE_BYTES = 16 * 1024 * 1024
_MAX_UPLOAD_PARTS = 10_000


class PresignedUrlMethod(StringEnum):
    GetObject = "get-object"
    HeadObject = "head-object"
    PutObject = "put-object"
    UploadPart = "upload-part"


class CloudBucketConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    provider: str = "s3"
    prefix: str = ""
    region: str | None = None
    endpoint: str | None = Field(
        default=None,
        validation_alias=AliasChoices("endpoint", "endpoint_url"),
    )
    read_only: bool = False
    force_path_style: bool = False
    access_key: str | None = None
    secret_key: str | None = None
    bucket: str | None = None

    @field_validator("prefix")
    @classmethod
    def prefix_must_be_mountpoint_compatible(cls, value: str) -> str:
        return normalize_mount_prefix(value)

    @model_validator(mode="after")
    def supported_provider_and_auth(self) -> CloudBucketConfig:
        if self.provider != "s3":
            msg = f"unsupported cloud bucket provider: {self.provider!r}"
            raise ValueError(msg)
        infer_mount_auth_mode(self.access_key, self.secret_key)
        return self

    def __init__(
        self,
        *,
        provider: str = "s3",
        prefix: str = "",
        region: str | None = None,
        endpoint: str | None = None,
        endpoint_url: str | None = None,
        read_only: bool = False,
        force_path_style: bool = False,
        access_key: str | None = None,
        secret_key: str | None = None,
        bucket: str | None = None,
    ) -> None:
        super().__init__(
            provider=provider,
            prefix=prefix,
            region=region,
            endpoint=endpoint if endpoint is not None else endpoint_url,
            read_only=read_only,
            force_path_style=force_path_style,
            access_key=access_key,
            secret_key=secret_key,
            bucket=bucket,
        )

    @property
    def endpoint_url(self) -> str | None:
        return self.endpoint

    @property
    def auth_mode(self) -> MountAuthMode:
        return infer_mount_auth_mode(self.access_key, self.secret_key)


@dataclass(frozen=True)
class VolumePathInfo:
    path: str
    size: int
    modified_at: datetime | None
    """Absent for directories."""
    is_dir: bool


@dataclass(frozen=True)
class VolumeFileServiceInfo:
    enabled: bool = True
    command_version: int = 1


@dataclass(frozen=True)
class PresignedUrl:
    method: PresignedUrlMethod
    url: str
    expires_seconds: int
    upload_id: str | None = None
    part_number: int | None = None


@dataclass(frozen=True)
class FileUploadPart:
    number: int
    start: int
    end: int
    url: str


@dataclass(frozen=True)
class MultipartUploadPlan:
    upload_id: str
    volume_name: str
    volume_path: str
    chunk_size: int
    file_size: int
    parts: list[FileUploadPart]


@dataclass(frozen=True)
class CompletedPart:
    number: int
    etag: str


@dataclass(frozen=True)
class MultipartCompletion:
    upload_id: str
    volume_name: str
    volume_path: str
    completed_parts: list[CompletedPart]


@dataclass(frozen=True)
class MultipartAbort:
    upload_id: str
    volume_name: str
    volume_path: str


@runtime_checkable
class VolumeExport(Protocol):
    def export(self) -> VolumeMount: ...


def volume_mounts(volumes: Iterable[VolumeMount | VolumeExport]) -> list[VolumeMount]:
    mounts: list[VolumeMount] = []
    for volume in volumes:
        if isinstance(volume, VolumeMount):
            mounts.append(volume)
            continue
        if isinstance(volume, VolumeExport):
            mounts.append(volume.export())
            continue
        msg = f"unsupported volume type: {type(volume).__name__}"
        raise TypeError(msg)
    return mounts


class VolumeOperationError(RuntimeError):
    pass


@dataclass(init=False, slots=True)
class CloudBucket:
    name: str
    mount_path: str
    config: CloudBucketConfig

    def __init__(self, name: str, mount_path: str, config: CloudBucketConfig) -> None:
        self.name = name
        self.mount_path = mount_path
        self.config = config

    def get_or_create(self) -> bool:
        return True

    def export(self) -> VolumeMount:
        return VolumeMount(
            name=self.name,
            mount_path=self.mount_path,
            read_only=self.config.read_only,
            config=_cloud_bucket_config(self.name, self.config),
        )


@dataclass(slots=True)
class Volume(ResourceControlBinding["StorageClient"]):
    name: str
    mount_path: str | None = None
    workspace: str | None = None
    ready: bool = field(default=False, init=False)
    volume_id: str | None = field(default=None, init=False)
    client: StorageClient | None = field(default=None, init=False, repr=False)
    endpoint: str | None = field(default=None, init=False, repr=False)
    token: str | None = field(default=None, init=False, repr=False)
    timeout_seconds: float = field(default=10.0, init=False, repr=False)

    @property
    def control_client(self) -> StorageClient:
        if self.client is None:
            self.client = storage_client(self._config())
        return self.client

    def get_or_create(self) -> bool:
        self.create()
        return True

    def create(self) -> api.Volume:
        """Create the volume, or return the one that already has this name."""
        volume = self.control_client.create_volume(self.name)
        self.ready = True
        self.volume_id = str(volume.id)
        return volume

    def export(self) -> VolumeMount:
        return VolumeMount(name=self.name, mount_path=str(self.path()))

    def path(self) -> Path:
        return Path(self.mount_path or _default_mount_path(self.name))

    def write_text(self, relative_path: str | Path, content: str) -> str:
        return self.write_bytes(relative_path, content.encode("utf-8"))

    def write_bytes(self, relative_path: str | Path, content: bytes) -> str:
        from lazycloud.clients.storage import put_presigned

        url = self.presigned_url(relative_path, method=PresignedUrlMethod.PutObject).url
        put_presigned(url, content)
        return self._volume_path(relative_path)

    def read_text(self, relative_path: str | Path, *, encoding: str = "utf-8") -> str:
        return self.read_bytes(relative_path).decode(encoding)

    def read_bytes(self, relative_path: str | Path) -> bytes:
        from lazycloud.clients.storage import get_presigned

        return get_presigned(self.presigned_url(relative_path).url)

    def list(self, relative_path: str | Path = ".") -> list[str]:
        return [item.path for item in self.list_path(relative_path)]

    def list_path(self, relative_path: str | Path = ".") -> list[VolumePathInfo]:
        """The entries of one directory; a missing directory is empty."""
        path = _relative_path(relative_path).as_posix()
        entries: list[VolumePathInfo] = []
        cursor: str | None = None
        while True:
            page = self.control_client.list_volume_files(self.name, path, cursor=cursor)
            entries.extend(_path_info(item) for item in page.files)
            if not page.next_cursor:
                return entries
            cursor = page.next_cursor

    def stat(self, relative_path: str | Path) -> VolumePathInfo:
        path = _relative_path(relative_path).as_posix()
        return _path_info(self.control_client.stat_volume_file(self.name, path))

    def put(self, source: str | Path, destination: str | Path | None = None) -> str:
        """Upload a file or a directory tree and return `<volume>/<destination>`.

        Files above 64 MiB go up as multipart uploads, a few parts at a time.
        """
        source_path = Path(source).expanduser().resolve()
        if not source_path.exists():
            raise FileNotFoundError(source_path)
        destination_root = _relative_path(destination or source_path.name)
        if source_path.is_file():
            return self._upload_file(source_path, destination_root.as_posix())
        for path in sorted(item for item in source_path.rglob("*") if item.is_file()):
            relative = path.relative_to(source_path)
            self._upload_file(path, (destination_root / relative.as_posix()).as_posix())
        return self._volume_path(destination_root.as_posix())

    def get(self, source: str | Path, destination: str | Path) -> Path:
        from lazycloud.clients.storage import download_presigned

        destination_path = Path(destination).expanduser().resolve()
        destination_path.parent.mkdir(parents=True, exist_ok=True)
        download_presigned(self.presigned_url(source).url, destination_path)
        return destination_path

    def move(self, source: str | Path, destination: str | Path) -> str:
        paths = _relative_path(source).as_posix(), _relative_path(destination).as_posix()
        self.control_client.move_volume_file(self.name, *paths)
        return self._volume_path(destination)

    def remove(self, relative_path: str | Path) -> tuple[str, ...]:
        """Remove a file, or a directory and everything in it; returns the removed files."""
        path = _relative_path(relative_path).as_posix()
        return tuple(self.control_client.remove_volume_files(self.name, path).removed)

    def delete(self) -> bool:
        """Delete the volume and its files. The name is free again at once."""
        self.control_client.delete_volume(self.name)
        self.ready = False
        self.volume_id = None
        return True

    def file_service_info(self) -> VolumeFileServiceInfo:
        return VolumeFileServiceInfo()

    def presigned_url(
        self,
        relative_path: str | Path,
        *,
        method: PresignedUrlMethod = PresignedUrlMethod.GetObject,
        expires_seconds: int = 3600,
        upload_id: str | None = None,
        part_number: int | None = None,
    ) -> PresignedUrl:
        from lazycloud.contracts import api

        methods = {
            PresignedUrlMethod.GetObject: api.Method.get,
            PresignedUrlMethod.HeadObject: api.Method.head,
            PresignedUrlMethod.PutObject: api.Method.put,
            PresignedUrlMethod.UploadPart: api.Method.upload_part,
        }
        fields: dict[str, object] = {
            "path": _relative_path(relative_path).as_posix(),
            "method": methods[method],
            "expires_seconds": expires_seconds,
            "upload_id": upload_id,
            "part_number": part_number,
        }
        request = api.PresignVolumeFileRequest.model_validate(
            {name: value for name, value in fields.items() if value is not None}
        )
        response = self.control_client.presign_volume_file(self.name, request)
        return PresignedUrl(
            method=method,
            url=response.url,
            expires_seconds=expires_seconds,
            upload_id=upload_id,
            part_number=part_number,
        )

    def create_multipart_upload(
        self,
        relative_path: str | Path,
        *,
        file_size: int,
        chunk_size: int = DEFAULT_MULTIPART_CHUNK_SIZE_BYTES,
    ) -> MultipartUploadPlan:
        """Start a multipart upload with every part presigned; the caller sends the parts."""
        upload = self._start_upload(relative_path, file_size=file_size, chunk_size=chunk_size)
        return MultipartUploadPlan(
            upload_id=upload.upload_id,
            volume_name=self.name,
            volume_path=upload.path,
            chunk_size=upload.part_size_bytes,
            file_size=file_size,
            parts=[
                FileUploadPart(
                    number=part.number,
                    start=part.offset,
                    end=part.offset + part.size_bytes,
                    url=part.url,
                )
                for part in upload.parts
            ],
        )

    def complete_multipart_upload(
        self,
        upload_id: str,
        relative_path: str | Path,
        completed_parts: list[CompletedPart],
    ) -> MultipartCompletion:
        from lazycloud.contracts import api

        path = _relative_path(relative_path).as_posix()
        self._complete_upload(
            upload_id,
            path,
            [api.CompletedPart(number=part.number, etag=part.etag) for part in completed_parts],
        )
        return MultipartCompletion(
            upload_id=upload_id,
            volume_name=self.name,
            volume_path=path,
            completed_parts=completed_parts,
        )

    def abort_multipart_upload(self, upload_id: str, relative_path: str | Path) -> MultipartAbort:
        from lazycloud.contracts import api

        path = _relative_path(relative_path).as_posix()
        self.control_client.abort_volume_upload(
            self.name, api.AbortVolumeUploadRequest(path=path, upload_id=upload_id)
        )
        return MultipartAbort(upload_id=upload_id, volume_name=self.name, volume_path=path)

    def _upload_file(self, source: Path, destination: str) -> str:
        size = source.stat().st_size
        if size <= MULTIPART_THRESHOLD_BYTES:
            return self.write_bytes(destination, source.read_bytes())
        part_size = max(_PUT_PART_SIZE_BYTES, math.ceil(size / _MAX_UPLOAD_PARTS))
        from lazycloud.clients.storage import upload_file_parts

        upload = self._start_upload(destination, file_size=size, chunk_size=part_size)
        try:
            parts = upload_file_parts(source, upload.parts)
            self._complete_upload(upload.upload_id, upload.path, parts)
        except BaseException:
            self.abort_multipart_upload(upload.upload_id, upload.path)
            raise
        return self._volume_path(destination)

    def _start_upload(
        self, relative_path: str | Path, *, file_size: int, chunk_size: int
    ) -> api.MultipartUpload:
        from lazycloud.contracts import api

        request = api.CreateVolumeUploadRequest(
            path=_relative_path(relative_path).as_posix(),
            size_bytes=file_size,
            part_size_bytes=chunk_size,
        )
        return self.control_client.create_volume_upload(self.name, request)

    def _complete_upload(self, upload_id: str, path: str, parts: list[api.CompletedPart]) -> None:
        from lazycloud.contracts import api

        self.control_client.complete_volume_upload(
            self.name, api.CompleteVolumeUploadRequest(path=path, upload_id=upload_id, parts=parts)
        )

    def _volume_path(self, relative_path: str | Path) -> str:
        relative = _relative_path(relative_path)
        if str(relative) == ".":
            return self.name
        return f"{self.name}/{relative.as_posix()}"


def _default_mount_path(name: str) -> str:
    return f"{DEFAULT_VOLUME_MOUNT_ROOT}/{name}"


def _cloud_bucket_config(name: str, config: CloudBucketConfig) -> dict[str, JsonValue]:
    return {
        "bucket_name": config.bucket or name,
        "prefix": config.prefix,
        "auth_mode": config.auth_mode,
        "access_key": config.access_key or "",
        "secret_key": config.secret_key or "",
        "endpoint_url": config.endpoint_url or "",
        "region": config.region or "",
        "read_only": config.read_only,
        "force_path_style": config.force_path_style,
    }


def _relative_path(value: str | Path | PurePosixPath) -> PurePosixPath:
    path = PurePosixPath(str(value).replace("\\", "/"))
    if path.is_absolute() or ".." in path.parts:
        msg = f"unsafe volume path: {value}"
        raise ValueError(msg)
    return path


def _path_info(info: api.VolumeFile) -> VolumePathInfo:
    return VolumePathInfo(
        path=info.path,
        size=info.size_bytes,
        modified_at=info.modified_at,
        is_dir=info.is_dir,
    )


__all__ = [
    "DEFAULT_MULTIPART_CHUNK_SIZE_BYTES",
    "DEFAULT_VOLUME_DOWNLOAD_TIMEOUT_SECONDS",
    "DEFAULT_VOLUME_MOUNT_ROOT",
    "MULTIPART_THRESHOLD_BYTES",
    "CloudBucket",
    "CloudBucketConfig",
    "CompletedPart",
    "FileUploadPart",
    "MultipartAbort",
    "MultipartCompletion",
    "MultipartUploadPlan",
    "PresignedUrl",
    "PresignedUrlMethod",
    "Volume",
    "VolumeExport",
    "VolumeFileServiceInfo",
    "VolumeOperationError",
    "VolumePathInfo",
]
