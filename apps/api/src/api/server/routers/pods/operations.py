from __future__ import annotations

from execution.pods.service import PodControlService
from fastapi import APIRouter, Depends, Response, status
from shared.http.pods import (
    PodSandboxConnectResponse,
    PodSandboxCreateImageFromFilesystemRequest,
    PodSandboxCreateImageFromFilesystemResponse,
    PodSandboxExposePortRequest,
    PodSandboxExposePortResponse,
    PodSandboxListUrlsResponse,
    PodSandboxSnapshotMemoryRequest,
    PodSandboxSnapshotMemoryResponse,
    PodSandboxUpdateNetworkPermissionsRequest,
    PodSandboxUpdateNetworkPermissionsResponse,
    PodSandboxUpdateTTLRequest,
    PodSandboxUpdateTTLResponse,
)

from api.server.routers.pods.common import read_container, write_container
from api.server.service_dependencies import pod_service

router = APIRouter(prefix="/api/v1/pods", tags=["pods"])


@router.post("/{container_id}/ports/expose", response_model=PodSandboxExposePortResponse)
def sandbox_expose_port(
    container_id: str,
    request: PodSandboxExposePortRequest,
    _auth: write_container,
    service: PodControlService = Depends(pod_service),
) -> PodSandboxExposePortResponse:
    return service.sandbox_expose_port(container_id, request)


@router.post(
    "/{container_id}/network/update",
    response_model=PodSandboxUpdateNetworkPermissionsResponse,
)
def sandbox_update_network_permissions(
    container_id: str,
    request: PodSandboxUpdateNetworkPermissionsRequest,
    _auth: write_container,
    service: PodControlService = Depends(pod_service),
) -> PodSandboxUpdateNetworkPermissionsResponse:
    return service.sandbox_update_network_permissions(container_id, request)


@router.get(
    "/{container_id}/network",
    response_model=PodSandboxUpdateNetworkPermissionsResponse,
    operation_id="get_sandbox_network_permissions",
)
def sandbox_network_permissions(
    container_id: str,
    _auth: read_container,
    service: PodControlService = Depends(pod_service),
) -> PodSandboxUpdateNetworkPermissionsResponse:
    return service.sandbox_network_permissions(container_id)


@router.post("/{container_id}/connect", response_model=PodSandboxConnectResponse)
def sandbox_connect(
    container_id: str,
    _auth: read_container,
    service: PodControlService = Depends(pod_service),
) -> PodSandboxConnectResponse:
    return service.sandbox_connect(container_id)


@router.post("/{container_id}/ttl", response_model=PodSandboxUpdateTTLResponse)
def sandbox_update_ttl(
    container_id: str,
    request: PodSandboxUpdateTTLRequest,
    _auth: write_container,
    service: PodControlService = Depends(pod_service),
) -> PodSandboxUpdateTTLResponse:
    return service.sandbox_update_ttl(container_id, request)


@router.post(
    "/{container_id}/terminate",
    operation_id="terminate_sandbox",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
)
def sandbox_terminate(
    container_id: str,
    _auth: write_container,
    service: PodControlService = Depends(pod_service),
) -> None:
    service.sandbox_terminate(container_id)


@router.post(
    "/{container_id}/create-image-from-filesystem",
    response_model=PodSandboxCreateImageFromFilesystemResponse,
)
def sandbox_create_image_from_filesystem(
    container_id: str,
    request: PodSandboxCreateImageFromFilesystemRequest,
    _auth: write_container,
    service: PodControlService = Depends(pod_service),
) -> PodSandboxCreateImageFromFilesystemResponse:
    return service.sandbox_create_image_from_filesystem(container_id, request)


@router.post("/{container_id}/snapshot-memory", response_model=PodSandboxSnapshotMemoryResponse)
def sandbox_snapshot_memory(
    container_id: str,
    request: PodSandboxSnapshotMemoryRequest,
    _auth: write_container,
    service: PodControlService = Depends(pod_service),
) -> PodSandboxSnapshotMemoryResponse:
    return service.sandbox_snapshot_memory(container_id, request)


@router.get("/{container_id}/urls", response_model=PodSandboxListUrlsResponse)
def sandbox_list_urls(
    container_id: str,
    _auth: read_container,
    service: PodControlService = Depends(pod_service),
) -> PodSandboxListUrlsResponse:
    return service.sandbox_list_urls(container_id)
