from __future__ import annotations

from control.storage_relationships import StorageRelationshipService
from execution.volumes.control import VolumeControlService
from fastapi import APIRouter, Depends, status
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
    GetFileServiceInfoResponse,
    GetOrCreateVolumeRequest,
    GetOrCreateVolumeResponse,
    ListPathRequest,
    ListPathResponse,
    ListVolumesResponse,
    MovePathRequest,
    MovePathResponse,
    StatPathRequest,
    StatPathResponse,
)

from api.server.auth import read_access, read_workspace, write_workspace
from api.server.dependencies import current_services
from api.server.service_dependencies import volume_service
from api.server.services import ApiServices

router = APIRouter(prefix="/api/v1/volumes", tags=["volume"])


@router.get("", response_model=ListVolumesResponse)
def list_volumes(
    workspace_id: read_workspace,
    service: VolumeControlService = Depends(volume_service),
    services: ApiServices = Depends(current_services),
) -> ListVolumesResponse:
    response = service.list_volumes(workspace_id=workspace_id)
    relationships = StorageRelationshipService(services.deployment_resources).for_workspace(
        workspace_id
    )
    return response.model_copy(
        update={
            "volumes": tuple(
                volume.model_copy(
                    update={"workloads": list(relationships.volumes.get(volume.name, ()))}
                )
                for volume in response.volumes
            )
        }
    )


@router.post("", response_model=GetOrCreateVolumeResponse, status_code=status.HTTP_201_CREATED)
def get_or_create_volume(
    request: GetOrCreateVolumeRequest,
    workspace_id: write_workspace,
    service: VolumeControlService = Depends(volume_service),
) -> GetOrCreateVolumeResponse:
    return service.get_or_create_volume(request, workspace_id=workspace_id)


@router.get("/file-service-info", response_model=GetFileServiceInfoResponse)
def get_file_service_info(
    _auth: read_access,
    service: VolumeControlService = Depends(volume_service),
) -> GetFileServiceInfoResponse:
    return service.get_file_service_info()


@router.post("/presigned-url", response_model=CreatePresignedUrlResponse)
def create_presigned_url(
    request: CreatePresignedUrlRequest,
    workspace_id: read_workspace,
    service: VolumeControlService = Depends(volume_service),
) -> CreatePresignedUrlResponse:
    return service.create_presigned_url(request, workspace_id=workspace_id)


@router.post("/multipart-upload", response_model=CreateMultipartUploadResponse)
def create_multipart_upload(
    request: CreateMultipartUploadRequest,
    workspace_id: write_workspace,
    service: VolumeControlService = Depends(volume_service),
) -> CreateMultipartUploadResponse:
    return service.create_multipart_upload(request, workspace_id=workspace_id)


@router.post("/multipart-upload/complete", response_model=CompleteMultipartUploadResponse)
def complete_multipart_upload(
    request: CompleteMultipartUploadRequest,
    workspace_id: write_workspace,
    service: VolumeControlService = Depends(volume_service),
) -> CompleteMultipartUploadResponse:
    return service.complete_multipart_upload(request, workspace_id=workspace_id)


@router.post("/multipart-upload/abort", response_model=AbortMultipartUploadResponse)
def abort_multipart_upload(
    request: AbortMultipartUploadRequest,
    workspace_id: write_workspace,
    service: VolumeControlService = Depends(volume_service),
) -> AbortMultipartUploadResponse:
    return service.abort_multipart_upload(request, workspace_id=workspace_id)


@router.post("/copy-path", response_model=CopyPathResponse)
def copy_path_stream(
    request: CopyPathBody,
    workspace_id: write_workspace,
    service: VolumeControlService = Depends(volume_service),
) -> CopyPathResponse:
    return service.copy_path(
        request.path,
        request.bytes_value(),
        workspace_id=workspace_id,
    )


@router.post("/{name}/delete", response_model=DeleteVolumeResponse)
def delete_volume(
    name: str,
    workspace_id: write_workspace,
    service: VolumeControlService = Depends(volume_service),
) -> DeleteVolumeResponse:
    return service.delete_volume(
        DeleteVolumeRequest(name=name),
        workspace_id=workspace_id,
    )


@router.get("/{path:path}/stat", response_model=StatPathResponse)
def stat_path(
    path: str,
    workspace_id: read_workspace,
    service: VolumeControlService = Depends(volume_service),
) -> StatPathResponse:
    return service.stat_path(StatPathRequest(path=path), workspace_id=workspace_id)


@router.post("/{path:path}/delete", response_model=DeletePathResponse)
def delete_path(
    path: str,
    workspace_id: write_workspace,
    service: VolumeControlService = Depends(volume_service),
) -> DeletePathResponse:
    return service.delete_path(DeletePathRequest(path=path), workspace_id=workspace_id)


@router.post("/{original_path:path}/move", response_model=MovePathResponse)
def move_path(
    original_path: str,
    request: MovePathRequest,
    workspace_id: write_workspace,
    service: VolumeControlService = Depends(volume_service),
) -> MovePathResponse:
    return service.move_path(
        MovePathRequest(original_path=original_path, new_path=request.new_path),
        workspace_id=workspace_id,
    )


@router.get("/{path:path}", response_model=ListPathResponse)
def list_path(
    path: str,
    workspace_id: read_workspace,
    service: VolumeControlService = Depends(volume_service),
) -> ListPathResponse:
    return service.list_path(ListPathRequest(path=path), workspace_id=workspace_id)
