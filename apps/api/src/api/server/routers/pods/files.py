from __future__ import annotations

from execution.pods.service import PodControlService
from fastapi import APIRouter, Depends, Query
from shared.http.pods import (
    POD_FILE_DOWNLOAD_MAX_BYTES,
    POD_FILE_LIST_MAX_ENTRIES,
    PodSandboxDeleteFileResponse,
    PodSandboxDownloadFileResponse,
    PodSandboxFindInFilesRequest,
    PodSandboxFindInFilesResponse,
    PodSandboxListFilesResponse,
    PodSandboxReplaceInFilesRequest,
    PodSandboxReplaceInFilesResponse,
    PodSandboxStatFileResponse,
    PodSandboxUploadFileBody,
    PodSandboxUploadFileResponse,
)

from api.server.auth import read_transfer, write_transfer
from api.server.routers.pods.common import read_container, write_container
from api.server.service_dependencies import pod_service

router = APIRouter(prefix="/api/v1/pods", tags=["pods"])


@router.post("/{container_id}/files/upload", response_model=PodSandboxUploadFileResponse)
def sandbox_upload_file(
    container_id: str,
    request: PodSandboxUploadFileBody,
    _auth: write_container,
    _transfer: write_transfer,
    service: PodControlService = Depends(pod_service),
) -> PodSandboxUploadFileResponse:
    return service.sandbox_upload_file(container_id, request)


@router.get(
    "/{container_id}/files/download",
    response_model=PodSandboxDownloadFileResponse,
)
def sandbox_download_file(
    container_id: str,
    container_path: str,
    max_bytes: int = Query(
        default=POD_FILE_DOWNLOAD_MAX_BYTES, ge=1, le=POD_FILE_DOWNLOAD_MAX_BYTES
    ),
    truncate: bool = False,
    *,
    _auth: read_container,
    _transfer: read_transfer,
    service: PodControlService = Depends(pod_service),
) -> PodSandboxDownloadFileResponse:
    """A file of at most `max_bytes`; with `truncate`, the first `max_bytes` of a larger one."""
    return service.sandbox_download_file(
        container_id, container_path, max_bytes=max_bytes, truncate=truncate
    )


@router.get("/{container_id}/files/stat", response_model=PodSandboxStatFileResponse)
def sandbox_stat_file(
    container_id: str,
    container_path: str,
    _auth: read_container,
    service: PodControlService = Depends(pod_service),
) -> PodSandboxStatFileResponse:
    return service.sandbox_stat_file(container_id, container_path)


@router.get("/{container_id}/files", response_model=PodSandboxListFilesResponse)
def sandbox_list_files(
    container_id: str,
    container_path: str = ".",
    limit: int = Query(default=POD_FILE_LIST_MAX_ENTRIES, ge=1, le=POD_FILE_LIST_MAX_ENTRIES),
    *,
    _auth: read_container,
    service: PodControlService = Depends(pod_service),
) -> PodSandboxListFilesResponse:
    """A directory's first `limit` entries by name."""
    return service.sandbox_list_files(container_id, container_path, limit=limit)


@router.delete(
    "/{container_id}/files",
    response_model=PodSandboxDeleteFileResponse,
)
def sandbox_delete_file(
    container_id: str,
    container_path: str,
    _auth: write_container,
    service: PodControlService = Depends(pod_service),
) -> PodSandboxDeleteFileResponse:
    return service.sandbox_delete_file(container_id, container_path)


@router.post("/{container_id}/files/replace", response_model=PodSandboxReplaceInFilesResponse)
def sandbox_replace_in_files(
    container_id: str,
    request: PodSandboxReplaceInFilesRequest,
    _auth: write_container,
    _transfer: write_transfer,
    service: PodControlService = Depends(pod_service),
) -> PodSandboxReplaceInFilesResponse:
    return service.sandbox_replace_in_files(container_id, request)


@router.post("/{container_id}/files/find", response_model=PodSandboxFindInFilesResponse)
def sandbox_find_in_files(
    container_id: str,
    request: PodSandboxFindInFilesRequest,
    _auth: read_container,
    _transfer: read_transfer,
    service: PodControlService = Depends(pod_service),
) -> PodSandboxFindInFilesResponse:
    return service.sandbox_find_in_files(container_id, request)
