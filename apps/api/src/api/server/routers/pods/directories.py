from __future__ import annotations

from execution.pods.service import PodControlService
from fastapi import APIRouter, Depends
from shared.http.pods import (
    PodSandboxCreateDirectoryRequest,
    PodSandboxCreateDirectoryResponse,
    PodSandboxDeleteDirectoryResponse,
)

from api.server.routers.pods.common import write_container
from api.server.service_dependencies import pod_service

router = APIRouter(prefix="/api/v1/pods", tags=["pods"])


@router.post("/{container_id}/directories", response_model=PodSandboxCreateDirectoryResponse)
def sandbox_create_directory(
    container_id: str,
    request: PodSandboxCreateDirectoryRequest,
    _auth: write_container,
    service: PodControlService = Depends(pod_service),
) -> PodSandboxCreateDirectoryResponse:
    return service.sandbox_create_directory(container_id, request)


@router.delete(
    "/{container_id}/directories",
    response_model=PodSandboxDeleteDirectoryResponse,
)
def sandbox_delete_directory(
    container_id: str,
    container_path: str,
    _auth: write_container,
    service: PodControlService = Depends(pod_service),
) -> PodSandboxDeleteDirectoryResponse:
    return service.sandbox_delete_directory(container_id, container_path)
