from __future__ import annotations

from datetime import datetime
from typing import Protocol

from pydantic import Field

from shared.bytes_transport import EncodedBytesBody
from shared.enums import StringEnum
from shared.http.base import HttpModel
from shared.http.storage import ResourceWorkloadReference


class PresignedUrlMethod(StringEnum):
    GetObject = "get-object"
    HeadObject = "head-object"
    UploadPart = "upload-part"


class _VolumeFileServiceInfoPlan(Protocol):
    enabled: bool
    command_version: int


class _VolumeUploadPartPlan(Protocol):
    number: int
    start: int
    end: int


class _VolumeMultipartUploadPlan(Protocol):
    upload_id: str
    parts: tuple[_VolumeUploadPartPlan, ...]


class VolumeInstance(HttpModel):
    id: str
    name: str
    size: int = Field(default=0, ge=0)
    created_at: datetime
    updated_at: datetime
    workspace_id: str
    workspace_name: str
    deletion_requested_at: datetime | None = None
    workloads: list[ResourceWorkloadReference] = Field(default_factory=list)


class GetOrCreateVolumeRequest(HttpModel):
    name: str


class GetOrCreateVolumeResponse(HttpModel):
    volume: VolumeInstance | None = None


class DeleteVolumeRequest(HttpModel):
    name: str


class DeleteVolumeResponse(HttpModel):
    deleted: bool = True


class PathInfo(HttpModel):
    path: str
    size: int = Field(default=0, ge=0)
    mod_time: datetime
    is_dir: bool


class ListPathRequest(HttpModel):
    path: str


class ListPathResponse(HttpModel):
    path_infos: tuple[PathInfo, ...] = ()


class DeletePathRequest(HttpModel):
    path: str


class DeletePathResponse(HttpModel):
    deleted: tuple[str, ...] = ()


class CopyPathBody(EncodedBytesBody):
    path: str


class CopyPathResponse(HttpModel):
    object_id: str = ""


class ListVolumesRequest(HttpModel):
    pass


class ListVolumesResponse(HttpModel):
    volumes: tuple[VolumeInstance, ...] = ()


class MovePathRequest(HttpModel):
    original_path: str
    new_path: str


class MovePathResponse(HttpModel):
    new_path: str = ""


class StatPathRequest(HttpModel):
    path: str


class StatPathResponse(HttpModel):
    path_info: PathInfo | None = None


class PresignedUrlParams(HttpModel):
    upload_id: str = ""
    part_number: int = Field(default=0, ge=0)


class GetFileServiceInfoRequest(HttpModel):
    pass


class GetFileServiceInfoResponse(HttpModel):
    enabled: bool = False
    command_version: int = 1

    @classmethod
    def from_plan(cls, plan: _VolumeFileServiceInfoPlan) -> GetFileServiceInfoResponse:
        return cls(enabled=plan.enabled, command_version=plan.command_version)


class CreatePresignedUrlRequest(HttpModel):
    volume_name: str
    volume_path: str
    expires: int = Field(default=0, ge=0)
    method: PresignedUrlMethod = PresignedUrlMethod.GetObject
    params: PresignedUrlParams = Field(default_factory=PresignedUrlParams)


class CreatePresignedUrlResponse(HttpModel):
    url: str = ""


class CreateMultipartUploadRequest(HttpModel):
    volume_name: str
    volume_path: str
    chunk_size: int = Field(default=0, ge=0)
    file_size: int = Field(default=0, ge=0)


class FileUploadPart(HttpModel):
    number: int
    start: int = Field(ge=0)
    end: int = Field(ge=0)
    url: str


class CreateMultipartUploadResponse(HttpModel):
    upload_id: str = ""
    file_upload_parts: tuple[FileUploadPart, ...] = ()

    @classmethod
    def from_plan(
        cls,
        plan: _VolumeMultipartUploadPlan,
        *,
        urls: tuple[str, ...],
    ) -> CreateMultipartUploadResponse:
        return cls(
            upload_id=plan.upload_id,
            file_upload_parts=tuple(
                FileUploadPart(
                    number=part.number,
                    start=part.start,
                    end=part.end,
                    url=url,
                )
                for part, url in zip(plan.parts, urls, strict=True)
            ),
        )


class CompletedPart(HttpModel):
    number: int
    etag: str


class CompleteMultipartUploadRequest(HttpModel):
    upload_id: str
    volume_name: str
    volume_path: str
    completed_parts: tuple[CompletedPart, ...] = ()


class CompleteMultipartUploadResponse(HttpModel):
    pass


class AbortMultipartUploadRequest(HttpModel):
    upload_id: str
    volume_name: str
    volume_path: str


class AbortMultipartUploadResponse(HttpModel):
    pass


__all__ = [
    "AbortMultipartUploadRequest",
    "AbortMultipartUploadResponse",
    "CompleteMultipartUploadRequest",
    "CompleteMultipartUploadResponse",
    "CompletedPart",
    "CopyPathBody",
    "CopyPathResponse",
    "CreateMultipartUploadRequest",
    "CreateMultipartUploadResponse",
    "CreatePresignedUrlRequest",
    "CreatePresignedUrlResponse",
    "DeletePathRequest",
    "DeletePathResponse",
    "DeleteVolumeRequest",
    "DeleteVolumeResponse",
    "FileUploadPart",
    "GetFileServiceInfoRequest",
    "GetFileServiceInfoResponse",
    "GetOrCreateVolumeRequest",
    "GetOrCreateVolumeResponse",
    "ListPathRequest",
    "ListPathResponse",
    "ListVolumesRequest",
    "ListVolumesResponse",
    "MovePathRequest",
    "MovePathResponse",
    "PathInfo",
    "PresignedUrlMethod",
    "PresignedUrlParams",
    "StatPathRequest",
    "StatPathResponse",
    "VolumeInstance",
]
