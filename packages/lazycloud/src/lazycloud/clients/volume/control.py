from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.parse import quote

from shared.bytes_transport import encode_bytes
from shared.http.volumes import (
    AbortMultipartUploadRequest,
    AbortMultipartUploadResponse,
    CompleteMultipartUploadRequest,
    CompleteMultipartUploadResponse,
    CopyPathBody,
    CopyPathResponse,
    CreateMultipartUploadRequest,
    CreateMultipartUploadResponse,
    CreatePresignedUrlRequest,
    CreatePresignedUrlResponse,
    DeletePathRequest,
    DeletePathResponse,
    DeleteVolumeRequest,
    DeleteVolumeResponse,
    GetFileServiceInfoRequest,
    GetFileServiceInfoResponse,
    GetOrCreateVolumeRequest,
    GetOrCreateVolumeResponse,
    ListPathRequest,
    ListPathResponse,
    ListVolumesRequest,
    ListVolumesResponse,
    MovePathRequest,
    MovePathResponse,
    PresignedUrlMethod,
    PresignedUrlParams,
    StatPathRequest,
    StatPathResponse,
)
from shared.http_transport import HttpChannel

from lazycloud.control import workspace_path


class VolumeControlChannel(Protocol):
    def get(self, path: str) -> Any: ...

    def post(self, path: str, payload: dict[str, Any] | None = None) -> Any: ...

    def delete(self, path: str) -> Any: ...


@dataclass
class VolumeControlClient:
    channel: VolumeControlChannel
    workspace: str = "default"

    @classmethod
    def from_endpoint(
        cls,
        endpoint: str,
        *,
        token: str | None = None,
        timeout_seconds: float = 10.0,
        workspace: str = "default",
    ) -> VolumeControlClient:
        return cls(
            channel=HttpChannel(endpoint=endpoint, token=token, timeout_seconds=timeout_seconds),
            workspace=workspace,
        )

    def create(self, name: str) -> GetOrCreateVolumeResponse:
        return self.get_or_create_volume(GetOrCreateVolumeRequest(name=name))

    def delete(self, name: str) -> DeleteVolumeResponse:
        return self.delete_volume(DeleteVolumeRequest(name=name))

    def copy(self, path: str, content: bytes) -> CopyPathResponse:
        return self.copy_path_stream(path, (content,))

    def stat(self, path: str) -> StatPathResponse:
        return self.stat_path(StatPathRequest(path=path))

    def move(self, original_path: str, new_path: str) -> MovePathResponse:
        return self.move_path(MovePathRequest(original_path=original_path, new_path=new_path))

    def presigned_url(
        self,
        volume_name: str,
        volume_path: str,
        *,
        method: PresignedUrlMethod = PresignedUrlMethod.GetObject,
        expires: int = 0,
        upload_id: str = "",
        part_number: int = 0,
        content_length: int = 0,
        content_type: str = "application/octet-stream",
    ) -> CreatePresignedUrlResponse:
        return self.create_presigned_url(
            CreatePresignedUrlRequest(
                volume_name=volume_name,
                volume_path=volume_path,
                expires=expires,
                method=method,
                params=PresignedUrlParams(
                    upload_id=upload_id,
                    part_number=part_number,
                    content_length=content_length,
                    content_type=content_type,
                ),
            )
        )

    def get_or_create_volume(
        self,
        request: GetOrCreateVolumeRequest,
    ) -> GetOrCreateVolumeResponse:
        return GetOrCreateVolumeResponse.model_validate(
            self.channel.post(self._path("/api/v1/volumes"), request.model_dump(mode="json"))
        )

    def delete_volume(self, request: DeleteVolumeRequest) -> DeleteVolumeResponse:
        return DeleteVolumeResponse.model_validate(
            self.channel.post(self._path(f"/api/v1/volumes/{quote(request.name, safe='')}/delete"))
        )

    def list_volumes(self, request: ListVolumesRequest | None = None) -> ListVolumesResponse:
        _ = request
        return ListVolumesResponse.model_validate(self.channel.get(self._path("/api/v1/volumes")))

    def list_path(self, request: ListPathRequest) -> ListPathResponse:
        return ListPathResponse.model_validate(
            self.channel.get(self._path(f"/api/v1/volumes/{self._quoted_path(request.path)}"))
        )

    def delete_path(self, request: DeletePathRequest) -> DeletePathResponse:
        return DeletePathResponse.model_validate(
            self.channel.post(
                self._path(f"/api/v1/volumes/{self._quoted_path(request.path)}/delete")
            )
        )

    def copy_path_stream(self, path: str, chunks: Iterable[bytes]) -> CopyPathResponse:
        body = CopyPathBody(
            path=path,
            value_base64=encode_bytes(b"".join(chunks)),
        )
        return CopyPathResponse.model_validate(
            self.channel.post(self._path("/api/v1/volumes/copy-path"), body.model_dump(mode="json"))
        )

    def move_path(self, request: MovePathRequest) -> MovePathResponse:
        return MovePathResponse.model_validate(
            self.channel.post(
                self._path(f"/api/v1/volumes/{self._quoted_path(request.original_path)}/move"),
                request.model_dump(mode="json"),
            )
        )

    def stat_path(self, request: StatPathRequest) -> StatPathResponse:
        return StatPathResponse.model_validate(
            self.channel.get(self._path(f"/api/v1/volumes/{self._quoted_path(request.path)}/stat"))
        )

    def get_file_service_info(
        self,
        request: GetFileServiceInfoRequest | None = None,
    ) -> GetFileServiceInfoResponse:
        _ = request
        return GetFileServiceInfoResponse.model_validate(
            self.channel.get(self._path("/api/v1/volumes/file-service-info"))
        )

    def create_presigned_url(
        self,
        request: CreatePresignedUrlRequest,
    ) -> CreatePresignedUrlResponse:
        return CreatePresignedUrlResponse.model_validate(
            self.channel.post(
                self._path("/api/v1/volumes/presigned-url"),
                request.model_dump(mode="json"),
            )
        )

    def create_multipart_upload(
        self,
        request: CreateMultipartUploadRequest,
    ) -> CreateMultipartUploadResponse:
        return CreateMultipartUploadResponse.model_validate(
            self.channel.post(
                self._path("/api/v1/volumes/multipart-upload"),
                request.model_dump(mode="json"),
            )
        )

    def complete_multipart_upload(
        self,
        request: CompleteMultipartUploadRequest,
    ) -> CompleteMultipartUploadResponse:
        return CompleteMultipartUploadResponse.model_validate(
            self.channel.post(
                self._path("/api/v1/volumes/multipart-upload/complete"),
                request.model_dump(mode="json"),
            )
        )

    def abort_multipart_upload(
        self,
        request: AbortMultipartUploadRequest,
    ) -> AbortMultipartUploadResponse:
        return AbortMultipartUploadResponse.model_validate(
            self.channel.post(
                self._path("/api/v1/volumes/multipart-upload/abort"),
                request.model_dump(mode="json"),
            )
        )

    def _path(self, path: str) -> str:
        return workspace_path(path, self.workspace)

    def _quoted_path(self, path: str) -> str:
        return quote(path, safe="")


__all__ = [
    "VolumeControlChannel",
    "VolumeControlClient",
]
