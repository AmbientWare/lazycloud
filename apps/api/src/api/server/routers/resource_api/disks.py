from __future__ import annotations

from fastapi import APIRouter, Depends, Response, status
from shared.http.disks import DiskListResponse, DiskResponse

from api.server.auth import read_workspace, write_workspace
from api.server.dependencies import current_services
from api.server.services import ApiServices

router = APIRouter(prefix="/api/v1/disks", tags=["disk"])


@router.get("", response_model=DiskListResponse, operation_id="list_disks")
def list_disks(
    workspace_id: read_workspace,
    cursor: str = "",
    limit: int = 100,
    services: ApiServices = Depends(current_services),
) -> DiskListResponse:
    page = services.disks.list(workspace_id=workspace_id, after=cursor, limit=limit)
    return DiskListResponse(
        data=[DiskResponse.from_record(record) for record in page.data],
        next=page.next,
    )


@router.get("/{name}", response_model=DiskResponse, operation_id="get_disk")
def get_disk(
    name: str,
    workspace_id: read_workspace,
    services: ApiServices = Depends(current_services),
) -> DiskResponse:
    return DiskResponse.from_record(services.disks.get(name, workspace_id=workspace_id))


@router.delete(
    "/{name}",
    operation_id="delete_disk",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
)
def delete_disk(
    name: str,
    workspace_id: write_workspace,
    services: ApiServices = Depends(current_services),
) -> None:
    services.disks.request_deletion(name, workspace_id=workspace_id)
