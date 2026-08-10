from __future__ import annotations

from collections.abc import Sequence
from urllib.parse import urlparse

from control.service import (
    ControlPlaneService,
    WorkspaceStorageAlreadyExistsError,
    WorkspaceStorageAuthorizationError,
    WorkspaceStorageError,
)
from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import Response
from gateway.service import GatewayControlService
from identity.workspaces import WorkspaceSettingsService
from shared.http.workspaces import (
    WorkspaceAuditEventResponse,
    WorkspaceAuditListResponse,
    WorkspaceConfigExportResponse,
    WorkspaceCreateRequest,
    WorkspaceListResponse,
    WorkspaceResponse,
    WorkspaceSetRequest,
    WorkspaceStorageRequest,
    WorkspaceUpdateRequest,
    workspace_response,
    workspace_storage_config,
)
from shared.identity import PlatformRole, WorkspaceRecord

from api.server.auth import admin_access, read_token, read_workspace, write_token, write_workspace
from api.server.dependencies import current_services, require_user_principal
from api.server.service_dependencies import control_plane_service, gateway_service
from api.server.services import ApiServices
from api.server.workspace_deletion import WorkspaceDeletionService

router = APIRouter()


def _storage_http_error(exc: Exception) -> HTTPException:
    if isinstance(exc, WorkspaceStorageAuthorizationError):
        return HTTPException(status.HTTP_401_UNAUTHORIZED, str(exc))
    if isinstance(exc, WorkspaceStorageAlreadyExistsError):
        return HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))
    if isinstance(exc, WorkspaceStorageError):
        return HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, str(exc))
    if isinstance(exc, KeyError):
        return HTTPException(status.HTTP_404_NOT_FOUND, str(exc))
    return HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, str(exc))


def _workspace_list_response(records: Sequence[WorkspaceRecord]) -> WorkspaceListResponse:
    return WorkspaceListResponse(workspaces=[workspace_response(item) for item in records])


@router.post(
    "/api/v1/workspaces",
    response_model=WorkspaceResponse,
    status_code=status.HTTP_201_CREATED,
    operation_id="create_workspace",
)
def api_v1_create_workspace(
    request: WorkspaceCreateRequest,
    _auth: admin_access,
    token: write_token,
    services: ApiServices = Depends(current_services),
    service: ControlPlaneService = Depends(control_plane_service),
) -> WorkspaceResponse:
    """Create a workspace owned by the account that asked for it.

    Ownership is the authenticated account rather than a name in the request, because
    the owner is who the workspace's compute account and domains resolve through and
    nothing has authenticated a name. A workspace-scoped automation credential is
    refused here for the same reason it is refused a cloud connection: it carries no
    authority over any account, so there would be nobody to own what it created.
    """
    try:
        storage = workspace_storage_config(request.storage) if request.storage is not None else None
        result = service.create_workspace(
            request.name,
            owner_user_id=require_user_principal(token),
            storage=storage,
        )
        return workspace_response(result.workspace)
    except (KeyError, WorkspaceStorageError) as exc:
        raise _storage_http_error(exc) from exc


@router.get(
    "/api/v1/workspaces",
    response_model=WorkspaceListResponse,
    operation_id="list_accessible_workspaces",
)
def api_v1_list_workspaces(
    include_deleted: bool = False,
    include_deleting: bool = False,
    *,
    token: read_token,
    services: ApiServices = Depends(current_services),
    service: ControlPlaneService = Depends(control_plane_service),
) -> WorkspaceListResponse:
    """The workspaces this caller may act on.

    Three answers, because there are three kinds of caller: an administrator sees
    every workspace, a person sees the ones they are a member of, and a
    workspace-scoped credential sees the single workspace it was minted for.
    """
    if services.auth.platform_role(token) is PlatformRole.Administrator:
        return _workspace_list_response(
            service.list_workspaces(
                include_deleted=include_deleted,
                include_deleting=include_deleting,
            )
        )
    if token.names_user and token.user_id:
        return _workspace_list_response(services.users.workspaces(token.user_id))
    return _workspace_list_response([service.get_workspace(token.workspace_id)])


@router.put(
    "/api/v1/workspaces/{name}",
    response_model=WorkspaceResponse,
    operation_id="upsert_workspace",
)
def upsert_workspace(
    name: str,
    request: WorkspaceSetRequest,
    _auth: admin_access,
    token: write_token,
    services: ApiServices = Depends(current_services),
    service: ControlPlaneService = Depends(control_plane_service),
) -> WorkspaceResponse:
    """Write a workspace's settings, creating it owned by the caller if it is new."""
    return workspace_response(
        service.set_workspace(
            name,
            owner_user_id=require_user_principal(token),
            storage=(
                workspace_storage_config(request.storage) if request.storage is not None else None
            ),
            signing_key_prefix=request.signing_key_prefix,
            primary_token_id=request.primary_token_id,
            labels=request.labels,
            metadata=request.metadata,
        )
    )


@router.get(
    "/api/v1/workspaces/current",
    response_model=WorkspaceResponse,
    operation_id="get_current_workspace",
)
def api_v1_current_workspace(
    workspace_id: read_workspace,
    service: ControlPlaneService = Depends(control_plane_service),
) -> WorkspaceResponse:
    return workspace_response(service.get_workspace(workspace_id))


@router.patch(
    "/api/v1/workspaces/current",
    response_model=WorkspaceResponse,
    operation_id="update_current_workspace",
)
def api_v1_update_current_workspace(
    request: WorkspaceUpdateRequest,
    workspace_id: write_workspace,
    token: write_token,
    services: ApiServices = Depends(current_services),
) -> WorkspaceResponse:
    return workspace_response(
        WorkspaceSettingsService(services.context).rename(
            workspace_id,
            name=request.name,
            actor=token,
        )
    )


@router.get(
    "/api/v1/workspaces/audit",
    response_model=WorkspaceAuditListResponse,
    operation_id="list_workspace_audit_history",
)
def api_v1_workspace_audit_history(
    limit: int = 50,
    cursor: str | None = None,
    *,
    workspace_id: read_workspace,
    services: ApiServices = Depends(current_services),
) -> WorkspaceAuditListResponse:
    result = WorkspaceSettingsService(services.context).audit_history(
        workspace_id,
        limit=limit,
        cursor=cursor,
    )
    return WorkspaceAuditListResponse(
        data=[
            WorkspaceAuditEventResponse.model_validate(record.model_dump(mode="json"))
            for record in result.page.records
        ],
        next=result.next,
    )


@router.get(
    "/api/v1/workspaces/export",
    response_model=WorkspaceConfigExportResponse,
    operation_id="export_workspace_config",
)
def api_v1_export_workspace_config(
    workspace_id: read_workspace,
    service: ControlPlaneService = Depends(control_plane_service),
    services: ApiServices = Depends(current_services),
) -> WorkspaceConfigExportResponse:
    parsed = urlparse(services.gateway_settings.public_http_url)
    scheme = parsed.scheme or "http"
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or (443 if scheme == "https" else 9000)
    return WorkspaceConfigExportResponse.model_validate(
        service.export_workspace_config(
            workspace_id,
            http_host=host,
            http_port=port,
            http_tls=scheme == "https",
            grpc_host=host,
            grpc_port=port + 1,
            grpc_tls=scheme == "https",
        )
    )


@router.post(
    "/api/v1/workspaces/set-external-storage",
    response_model=WorkspaceResponse,
    status_code=status.HTTP_201_CREATED,
    operation_id="set_external_workspace_storage",
)
def api_v1_set_external_workspace_storage(
    request: WorkspaceStorageRequest,
    workspace_id: write_workspace,
    token: write_token,
    service: ControlPlaneService = Depends(control_plane_service),
) -> WorkspaceResponse:
    try:
        return workspace_response(
            service.attach_external_workspace_storage(
                workspace_id,
                request.workspace_storage(),
                token_id_for_cache_invalidation=token.id,
            )
        )
    except (
        KeyError,
        WorkspaceStorageAlreadyExistsError,
        WorkspaceStorageAuthorizationError,
        WorkspaceStorageError,
    ) as exc:
        raise _storage_http_error(exc) from exc


@router.get(
    "/api/v1/workspaces/{workspace_id_or_name}",
    response_model=WorkspaceResponse,
    operation_id="get_workspace",
)
def get_workspace(
    workspace_id_or_name: str,
    _auth: admin_access,
    service: ControlPlaneService = Depends(control_plane_service),
) -> WorkspaceResponse:
    return workspace_response(service.get_workspace(workspace_id_or_name))


@router.delete(
    "/api/v1/workspaces/{workspace_id_or_name}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    operation_id="delete_workspace",
)
def delete_workspace(
    workspace_id_or_name: str,
    _auth: admin_access,
    token: write_token,
    services: ApiServices = Depends(current_services),
    gateway: GatewayControlService = Depends(gateway_service),
) -> None:
    WorkspaceDeletionService(services, gateway).delete(
        workspace_id_or_name,
        audit_actor=token,
    )


@router.post(
    "/api/v1/workspaces/create-storage",
    response_model=WorkspaceResponse,
    status_code=status.HTTP_201_CREATED,
    operation_id="create_workspace_storage",
)
def api_v1_create_workspace_storage(
    workspace_id: write_workspace,
    token: write_token,
    service: ControlPlaneService = Depends(control_plane_service),
) -> WorkspaceResponse:
    try:
        return workspace_response(
            service.create_workspace_storage(
                workspace_id,
                token_id_for_cache_invalidation=token.id,
            )
        )
    except (
        KeyError,
        WorkspaceStorageAlreadyExistsError,
        WorkspaceStorageAuthorizationError,
        WorkspaceStorageError,
    ) as exc:
        raise _storage_http_error(exc) from exc
