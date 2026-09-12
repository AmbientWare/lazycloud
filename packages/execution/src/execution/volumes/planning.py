from __future__ import annotations

import math
import posixpath
from enum import StrEnum

from shared.contracts import ContractModel

VOLUME_PRESIGNED_URL_DEFAULT_EXPIRES_SECONDS = 300
VOLUME_PRESIGNED_URL_MAX_EXPIRES_SECONDS = 604800


class VolumeMultipartStatus(StrEnum):
    Ready = "ready"
    InvalidFileSize = "invalid-file-size"
    InvalidChunkSize = "invalid-chunk-size"


class VolumeInputPath(ContractModel):
    volume_name: str
    volume_path: str


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
    "VOLUME_PRESIGNED_URL_DEFAULT_EXPIRES_SECONDS",
    "VOLUME_PRESIGNED_URL_MAX_EXPIRES_SECONDS",
    "VolumeFileServiceInfoPlan",
    "VolumeFileUploadPartPlan",
    "VolumeInputPath",
    "VolumeMultipartStatus",
    "VolumeMultipartUploadPlan",
    "clamp_presigned_url_expires",
    "parse_volume_input",
    "plan_volume_file_service_info",
    "plan_volume_multipart_upload",
]
