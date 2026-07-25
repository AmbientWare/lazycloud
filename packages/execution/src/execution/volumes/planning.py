from __future__ import annotations

import math
import posixpath
from enum import StrEnum

from shared.contracts import ContractModel

DEFAULT_VOLUMES_PATH = "/data/volumes"
VOLUME_PRESIGNED_URL_DEFAULT_EXPIRES_SECONDS = 300
VOLUME_PRESIGNED_URL_MAX_EXPIRES_SECONDS = 604800


class VolumePathStatus(StrEnum):
    Valid = "valid"
    MissingVolumeName = "missing-volume-name"
    EscapesRoot = "escapes-root"


class VolumeMultipartStatus(StrEnum):
    Ready = "ready"
    InvalidFileSize = "invalid-file-size"
    InvalidChunkSize = "invalid-chunk-size"


class VolumeInputPath(ContractModel):
    volume_name: str
    volume_path: str


class VolumePathPlan(ContractModel):
    status: VolumePathStatus
    workspace_id: str
    volume_id: str
    volume_name: str
    volume_path: str
    root_path: str
    full_path: str
    error_message: str = ""

    @property
    def ok(self) -> bool:
        return self.status is VolumePathStatus.Valid


class VolumeFileServiceInfoPlan(ContractModel):
    enabled: bool
    command_version: int = 1


class VolumeFileUploadPartPlan(ContractModel):
    number: int
    start: int
    end: int


class VolumeMultipartUploadPlan(ContractModel):
    status: VolumeMultipartStatus
    upload_id: str
    volume_name: str
    volume_path: str
    chunk_size: int
    file_size: int
    parts: tuple[VolumeFileUploadPartPlan, ...]
    error_message: str = ""

    @property
    def ok(self) -> bool:
        return self.status is VolumeMultipartStatus.Ready


def parse_volume_input(input_path: str) -> VolumeInputPath:
    volume_name, separator, rest = input_path.partition("/")
    volume_path = posixpath.normpath(rest) if separator else "."
    return VolumeInputPath(volume_name=volume_name, volume_path=volume_path)


def join_clean_path(*parts: str) -> str:
    cleaned = [posixpath.normpath(part) for part in parts if part != ""]
    return posixpath.normpath(posixpath.join(*cleaned)) if cleaned else "."


def join_volume_path(
    workspace_id: str,
    volume_id: str,
    *sub_paths: str,
    volumes_path: str = DEFAULT_VOLUMES_PATH,
) -> str:
    return join_clean_path(volumes_path, workspace_id, volume_id, *sub_paths)


def plan_volume_path(
    workspace_id: str,
    volume_id: str,
    input_path: str,
    *,
    volumes_path: str = DEFAULT_VOLUMES_PATH,
) -> VolumePathPlan:
    parsed = parse_volume_input(input_path)
    root_path = join_volume_path(workspace_id, volume_id, volumes_path=volumes_path)
    full_path = join_volume_path(
        workspace_id,
        volume_id,
        parsed.volume_path,
        volumes_path=volumes_path,
    )
    if parsed.volume_name == "":
        return VolumePathPlan(
            status=VolumePathStatus.MissingVolumeName,
            workspace_id=workspace_id,
            volume_id=volume_id,
            volume_name=parsed.volume_name,
            volume_path=parsed.volume_path,
            root_path=root_path,
            full_path=full_path,
            error_message="must provide volume name",
        )
    if posixpath.commonpath([root_path, full_path]) != root_path:
        return VolumePathPlan(
            status=VolumePathStatus.EscapesRoot,
            workspace_id=workspace_id,
            volume_id=volume_id,
            volume_name=parsed.volume_name,
            volume_path=parsed.volume_path,
            root_path=root_path,
            full_path=full_path,
            error_message="parent directory does not exist",
        )
    return VolumePathPlan(
        status=VolumePathStatus.Valid,
        workspace_id=workspace_id,
        volume_id=volume_id,
        volume_name=parsed.volume_name,
        volume_path=parsed.volume_path,
        root_path=root_path,
        full_path=full_path,
    )


def clamp_presigned_url_expires(expires_seconds: int) -> int:
    if expires_seconds <= 0:
        return VOLUME_PRESIGNED_URL_DEFAULT_EXPIRES_SECONDS
    return min(expires_seconds, VOLUME_PRESIGNED_URL_MAX_EXPIRES_SECONDS)


def plan_volume_file_service_info(*, enabled: bool) -> VolumeFileServiceInfoPlan:
    return VolumeFileServiceInfoPlan(enabled=enabled)


def plan_volume_multipart_upload(
    *,
    upload_id: str,
    volume_name: str,
    volume_path: str,
    file_size: int,
    chunk_size: int,
) -> VolumeMultipartUploadPlan:
    if file_size < 0:
        return VolumeMultipartUploadPlan(
            status=VolumeMultipartStatus.InvalidFileSize,
            upload_id=upload_id,
            volume_name=volume_name,
            volume_path=volume_path,
            chunk_size=chunk_size,
            file_size=file_size,
            parts=(),
            error_message="file_size cannot be negative",
        )
    if chunk_size <= 0 and not (file_size == 0 and chunk_size == 0):
        return VolumeMultipartUploadPlan(
            status=VolumeMultipartStatus.InvalidChunkSize,
            upload_id=upload_id,
            volume_name=volume_name,
            volume_path=volume_path,
            chunk_size=chunk_size,
            file_size=file_size,
            parts=(),
            error_message="chunk_size must be greater than zero",
        )

    effective_chunk_size = chunk_size
    total_parts = 1 if file_size == 0 else math.ceil(file_size / chunk_size)
    return VolumeMultipartUploadPlan(
        status=VolumeMultipartStatus.Ready,
        upload_id=upload_id,
        volume_name=volume_name,
        volume_path=volume_path,
        chunk_size=chunk_size,
        file_size=file_size,
        parts=tuple(
            VolumeFileUploadPartPlan(
                number=index + 1,
                start=index * effective_chunk_size,
                end=min((index + 1) * effective_chunk_size, file_size),
            )
            for index in range(total_parts)
        ),
    )


__all__ = [
    "DEFAULT_VOLUMES_PATH",
    "VOLUME_PRESIGNED_URL_DEFAULT_EXPIRES_SECONDS",
    "VOLUME_PRESIGNED_URL_MAX_EXPIRES_SECONDS",
    "VolumeFileServiceInfoPlan",
    "VolumeFileUploadPartPlan",
    "VolumeInputPath",
    "VolumeMultipartStatus",
    "VolumeMultipartUploadPlan",
    "VolumePathPlan",
    "VolumePathStatus",
    "clamp_presigned_url_expires",
    "join_clean_path",
    "join_volume_path",
    "parse_volume_input",
    "plan_volume_file_service_info",
    "plan_volume_multipart_upload",
    "plan_volume_path",
]
