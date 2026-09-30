from __future__ import annotations

import urllib.request
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Protocol, runtime_checkable

from pydantic import (
    AliasChoices,
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    field_validator,
    model_validator,
)
from shared.deployment_records import VolumeMount
from shared.http import volumes
from shared.http.volumes import (
    AbortMultipartUploadRequest,
    AbortMultipartUploadResponse,
    CompleteMultipartUploadRequest,
    CompleteMultipartUploadResponse,
    CopyPathResponse,
    CreateMultipartUploadRequest,
    CreateMultipartUploadResponse,
    CreatePresignedUrlResponse,
    DeletePathRequest,
    DeletePathResponse,
    DeleteVolumeResponse,
    GetFileServiceInfoResponse,
    GetOrCreateVolumeResponse,
    ListPathRequest,
    ListPathResponse,
    MovePathRequest,
    MovePathResponse,
    PathInfo,
    PresignedUrlMethod,
    StatPathRequest,
    StatPathResponse,
    VolumeInstance,
)
from shared.mounts import MountAuthMode, infer_mount_auth_mode, normalize_mount_prefix

from lazycloud.control import (
    ControlClientConfig,
    ResourceControlBinding,
    resolve_control_client_config,
)

DEFAULT_VOLUME_MOUNT_ROOT = "/volumes"
DEFAULT_MULTIPART_CHUNK_SIZE_BYTES = 5 * 1024 * 1024
DEFAULT_VOLUME_DOWNLOAD_TIMEOUT_SECONDS = 30.0


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
    modified_at: datetime
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


class VolumeClient(Protocol):
    def create(self, name: str) -> GetOrCreateVolumeResponse: ...

    def delete(self, name: str) -> DeleteVolumeResponse: ...

    def copy(self, path: str, content: bytes) -> CopyPathResponse: ...

    def list_path(self, request: ListPathRequest) -> ListPathResponse: ...

    def stat_path(self, request: StatPathRequest) -> StatPathResponse: ...

    def move_path(self, request: MovePathRequest) -> MovePathResponse: ...

    def delete_path(self, request: DeletePathRequest) -> DeletePathResponse: ...

    def get_file_service_info(self) -> GetFileServiceInfoResponse: ...

    def presigned_url(
        self,
        volume_name: str,
        volume_path: str,
        *,
        method: PresignedUrlMethod = PresignedUrlMethod.GetObject,
        expires: int = 0,
        upload_id: str = "",
        part_number: int = 0,
    ) -> CreatePresignedUrlResponse: ...

    def create_multipart_upload(
        self,
        request: CreateMultipartUploadRequest,
    ) -> CreateMultipartUploadResponse: ...

    def complete_multipart_upload(
        self,
        request: CompleteMultipartUploadRequest,
    ) -> CompleteMultipartUploadResponse: ...

    def abort_multipart_upload(
        self,
        request: AbortMultipartUploadRequest,
    ) -> AbortMultipartUploadResponse: ...


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
class Volume(ResourceControlBinding[VolumeClient]):
    name: str
    mount_path: str | None = None
    workspace: str | None = None
    ready: bool = field(default=False, init=False)
    volume_id: str | None = field(default=None, init=False)
    client: VolumeClient | None = field(default=None, init=False, repr=False)
    endpoint: str | None = field(default=None, init=False, repr=False)
    token: str | None = field(default=None, init=False, repr=False)
    timeout_seconds: float = field(default=10.0, init=False, repr=False)

    @property
    def control_client(self) -> VolumeClient:
        if self.client is None:
            config = resolve_control_client_config(
                workspace=self.workspace,
                endpoint=self.endpoint,
                token=self.token,
                timeout_seconds=self.timeout_seconds,
            )
            self.client = _default_volume_client(config)
        return self.client

    def get_or_create(self) -> bool:
        response = self.control_client.create(self.name)
        if response.volume is None:
            return False
        self.ready = True
        self.volume_id = response.volume.id
        return True

    def create(self) -> VolumeInstance:
        response = self.control_client.create(self.name)
        if response.volume is None:
            raise VolumeOperationError(f"failed to create volume: {self.name}")
        self.ready = True
        self.volume_id = response.volume.id
        return response.volume

    def export(self) -> VolumeMount:
        return VolumeMount(name=self.name, mount_path=str(self.path()))

    def path(self) -> Path:
        return Path(self.mount_path or _default_mount_path(self.name))

    def write_text(self, relative_path: str | Path, content: str) -> str:
        return self.write_bytes(relative_path, content.encode("utf-8"))

    def write_bytes(self, relative_path: str | Path, content: bytes) -> str:
        target = self._volume_path(relative_path)
        self.control_client.copy(target, content)
        return target

    def read_text(self, relative_path: str | Path, *, encoding: str = "utf-8") -> str:
        return self.read_bytes(relative_path).decode(encoding)

    def read_bytes(self, relative_path: str | Path) -> bytes:
        url = self.presigned_url(relative_path, method=PresignedUrlMethod.GetObject).url
        with urllib.request.urlopen(
            url,
            timeout=DEFAULT_VOLUME_DOWNLOAD_TIMEOUT_SECONDS,
        ) as response:
            return response.read()

    def list(self, relative_path: str | Path = ".") -> list[str]:
        return [item.path for item in self.list_path(relative_path)]

    def list_path(self, relative_path: str | Path = ".") -> list[VolumePathInfo]:
        path = self._volume_path(relative_path)
        response = self.control_client.list_path(ListPathRequest(path=path))
        return [_path_info(item) for item in response.path_infos]

    def stat(self, relative_path: str | Path) -> VolumePathInfo:
        path = self._volume_path(relative_path)
        response = self.control_client.stat_path(StatPathRequest(path=path))
        if response.path_info is None:
            raise VolumeOperationError(f"failed to stat volume path: {path}")
        return _path_info(response.path_info)

    def put(self, source: str | Path, destination: str | Path | None = None) -> str:
        source_path = Path(source).expanduser().resolve()
        if not source_path.exists():
            raise FileNotFoundError(source_path)
        destination_root = _relative_path(destination or source_path.name)
        if source_path.is_file():
            return self.write_bytes(str(destination_root), source_path.read_bytes())
        for path in sorted(item for item in source_path.rglob("*") if item.is_file()):
            relative = path.relative_to(source_path)
            self.write_bytes(
                str(PurePosixPath(destination_root, relative.as_posix())),
                path.read_bytes(),
            )
        return self._volume_path(str(destination_root))

    def get(self, source: str | Path, destination: str | Path) -> Path:
        destination_path = Path(destination).expanduser().resolve()
        destination_path.parent.mkdir(parents=True, exist_ok=True)
        destination_path.write_bytes(self.read_bytes(source))
        return destination_path

    def move(self, source: str | Path, destination: str | Path) -> str:
        source_path = self._volume_path(source)
        destination_path = self._volume_path(destination)
        response = self.control_client.move_path(
            MovePathRequest(original_path=source_path, new_path=destination_path)
        )
        return response.new_path or destination_path

    def remove(self, relative_path: str | Path) -> tuple[str, ...]:
        path = self._volume_path(relative_path)
        response = self.control_client.delete_path(DeletePathRequest(path=path))
        return response.deleted

    def delete(self) -> bool:
        response = self.control_client.delete(self.name)
        self.ready = False
        self.volume_id = None
        return response.deleted

    def file_service_info(self) -> VolumeFileServiceInfo:
        response = self.control_client.get_file_service_info()
        return VolumeFileServiceInfo(
            enabled=response.enabled,
            command_version=response.command_version,
        )

    def presigned_url(
        self,
        relative_path: str | Path,
        *,
        method: PresignedUrlMethod = PresignedUrlMethod.GetObject,
        expires_seconds: int = 3600,
        upload_id: str | None = None,
        part_number: int | None = None,
    ) -> PresignedUrl:
        response = self.control_client.presigned_url(
            self.name,
            str(_relative_path(relative_path)),
            method=method,
            expires=expires_seconds,
            upload_id=upload_id or "",
            part_number=part_number or 0,
        )
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
        response = self.control_client.create_multipart_upload(
            CreateMultipartUploadRequest(
                volume_name=self.name,
                volume_path=str(_relative_path(relative_path)),
                file_size=file_size,
                chunk_size=chunk_size,
            )
        )
        return MultipartUploadPlan(
            upload_id=response.upload_id,
            volume_name=self.name,
            volume_path=str(_relative_path(relative_path)),
            chunk_size=chunk_size,
            file_size=file_size,
            parts=[_file_upload_part(part) for part in response.file_upload_parts],
        )

    def complete_multipart_upload(
        self,
        upload_id: str,
        relative_path: str | Path,
        completed_parts: list[CompletedPart],
    ) -> MultipartCompletion:
        self.control_client.complete_multipart_upload(
            CompleteMultipartUploadRequest(
                upload_id=upload_id,
                volume_name=self.name,
                volume_path=str(_relative_path(relative_path)),
                completed_parts=tuple(
                    volumes.CompletedPart(number=part.number, etag=part.etag)
                    for part in completed_parts
                ),
            )
        )
        return MultipartCompletion(
            upload_id=upload_id,
            volume_name=self.name,
            volume_path=str(_relative_path(relative_path)),
            completed_parts=completed_parts,
        )

    def abort_multipart_upload(self, upload_id: str, relative_path: str | Path) -> MultipartAbort:
        self.control_client.abort_multipart_upload(
            AbortMultipartUploadRequest(
                upload_id=upload_id,
                volume_name=self.name,
                volume_path=str(_relative_path(relative_path)),
            )
        )
        return MultipartAbort(
            upload_id=upload_id,
            volume_name=self.name,
            volume_path=str(_relative_path(relative_path)),
        )

    def _volume_path(self, relative_path: str | Path) -> str:
        relative = _relative_path(relative_path)
        if str(relative) == ".":
            return self.name
        return f"{self.name}/{relative.as_posix()}"


def _default_volume_client(config: ControlClientConfig) -> VolumeClient:
    from lazycloud.clients.volume.control import VolumeControlClient

    return VolumeControlClient.from_endpoint(
        config.endpoint,
        token=config.token,
        timeout_seconds=config.timeout_seconds,
        workspace=config.workspace,
    )


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


def _path_info(info: PathInfo) -> VolumePathInfo:
    return VolumePathInfo(
        path=info.path,
        size=info.size,
        modified_at=info.mod_time,
        is_dir=info.is_dir,
    )


def _file_upload_part(part: volumes.FileUploadPart) -> FileUploadPart:
    return FileUploadPart(
        number=part.number,
        start=part.start,
        end=part.end,
        url=part.url,
    )


__all__ = [
    "DEFAULT_MULTIPART_CHUNK_SIZE_BYTES",
    "DEFAULT_VOLUME_DOWNLOAD_TIMEOUT_SECONDS",
    "DEFAULT_VOLUME_MOUNT_ROOT",
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
    "VolumeClient",
    "VolumeExport",
    "VolumeFileServiceInfo",
    "VolumeOperationError",
    "VolumePathInfo",
]
