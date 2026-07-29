from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query, Response, status
from identity.authz import token_has_scope
from shared.containers import ContainerStatus
from shared.http.compute import (
    ContainerDetailResponse,
    ContainerResponse,
    ContainerRunRequest,
    ContainerStopAllResponse,
    ContainerWithAppPageResponse,
    ContainerWithAppResponse,
)
from shared.identity import AuthScope

from api.server.auth import admin_access, read_token, read_workspace, write_workspace
from api.server.dependencies import current_services
from api.server.identifiers import identifier_filter
from api.server.routers.resource_api.common import _management
from api.server.services import ApiServices

router = APIRouter()


@router.get(
    "/api/v1/containers",
    response_model=ContainerWithAppPageResponse,
    operation_id="list_containers",
)
def list_containers(
    stub_ids: Annotated[list[str], Query(default_factory=list, alias="stub_id")],
    statuses: Annotated[list[ContainerStatus], Query(default_factory=list, alias="status")],
    app_id: identifier_filter = None,
    limit: int = Query(default=100, ge=1, le=100),
    cursor: str | None = Query(default=None, max_length=1024),
    *,
    workspace_id: read_workspace,
    services: ApiServices = Depends(current_services),
) -> ContainerWithAppPageResponse:
    page = _management(services).container_page(
        workspace_id,
        app_id=app_id,
        stub_ids=tuple(stub_ids),
        statuses=tuple(statuses),
        limit=limit,
        cursor=cursor,
    )
    return ContainerWithAppPageResponse(
        data=[ContainerWithAppResponse.model_validate(item) for item in page.data],
        next=page.next,
    )


@router.post(
    "/api/v1/containers",
    response_model=ContainerResponse,
    status_code=status.HTTP_201_CREATED,
    operation_id="run_container",
)
def run_container(
    request: ContainerRunRequest,
    workspace_id: write_workspace,
    services: ApiServices = Depends(current_services),
) -> ContainerResponse:
    return ContainerResponse.model_validate(
        services.containers.run(
            request.name,
            request.image,
            request.command,
            workspace_id=workspace_id,
            cwd=request.cwd,
            env=request.env,
            ports=request.ports,
            timeout_seconds=request.timeout_seconds,
        )
    )


@router.get(
    "/api/v1/containers/admin/{container_id}",
    response_model=ContainerResponse,
    operation_id="get_container_as_admin",
)
def get_container_as_admin(
    container_id: str,
    _auth: admin_access,
    services: ApiServices = Depends(current_services),
) -> ContainerResponse:
    return ContainerResponse.model_validate(_management(services).get_container(container_id))


@router.post(
    "/api/v1/containers/stop-all",
    response_model=ContainerStopAllResponse,
    operation_id="stop_all_containers",
)
def stop_all_containers(
    workspace_id: write_workspace,
    services: ApiServices = Depends(current_services),
) -> ContainerStopAllResponse:
    return ContainerStopAllResponse(
        message="all containers stopped",
        containers=[
            ContainerResponse.model_validate(item)
            for item in _management(services).stop_all_containers(workspace_id)
        ],
    )


@router.get(
    "/api/v1/containers/{container_id}",
    response_model=ContainerDetailResponse,
    operation_id="get_container",
)
def get_container(
    container_id: str,
    workspace_id: read_workspace,
    token: read_token,
    services: ApiServices = Depends(current_services),
) -> ContainerDetailResponse:
    return ContainerDetailResponse.model_validate(
        _management(services).container_view(
            container_id,
            workspace=workspace_id,
            can_write=token_has_scope(token, AuthScope.Write),
        )
    )


@router.post(
    "/api/v1/containers/{container_id}/stop",
    response_model=ContainerResponse,
    operation_id="stop_container",
)
def stop_container(
    container_id: str,
    workspace_id: write_workspace,
    services: ApiServices = Depends(current_services),
) -> ContainerResponse:
    return ContainerResponse.model_validate(
        _management(services).stop_container(workspace_id, container_id)
    )


@router.delete(
    "/api/v1/containers/{container_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    operation_id="delete_container",
)
def delete_container(
    container_id: str,
    workspace_id: write_workspace,
    services: ApiServices = Depends(current_services),
) -> None:
    _management(services).delete_container(workspace_id, container_id)
