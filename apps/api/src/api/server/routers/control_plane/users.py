from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Response, status
from shared.errors import NotFoundError
from shared.http.users import (
    PasswordChangeRequest,
    UserCreateRequest,
    UserListResponse,
    UserResponse,
    UserStatusRequest,
    WorkspaceMemberAddRequest,
    WorkspaceMemberListResponse,
    WorkspaceMemberResponse,
    WorkspaceMemberRoleRequest,
)
from shared.identity import (
    AuthScope,
    AuthTokenRecord,
    PlatformRole,
    UserRecord,
    WorkspaceMemberRecord,
    WorkspaceRole,
)

from api.server.auth import (
    admin_access,
    read_principal,
    read_token,
    write_principal,
    write_token,
)
from api.server.dependencies import (
    authorize_token_workspace,
    current_services,
    require_user_principal,
)
from api.server.services import ApiServices

router = APIRouter()


def user_response(record: UserRecord) -> UserResponse:
    """Project a user for the API, dropping the credential material it carries."""
    return UserResponse(
        id=record.id,
        username=record.username,
        role=record.role,
        status=record.status,
        created_at=record.created_at,
        updated_at=record.updated_at,
    )


def _member_response(
    membership: WorkspaceMemberRecord,
    username: str,
) -> WorkspaceMemberResponse:
    return WorkspaceMemberResponse(
        user_id=membership.user_id,
        username=username,
        role=membership.role,
        created_at=membership.created_at,
    )


@router.post(
    "/api/v1/users",
    response_model=UserResponse,
    status_code=status.HTTP_201_CREATED,
    operation_id="create_user",
)
def create_user(
    request: UserCreateRequest,
    _auth: admin_access,
    services: ApiServices = Depends(current_services),
) -> UserResponse:
    return user_response(
        services.users.create(
            username=request.username,
            password=request.password.get_secret_value(),
            role=request.role,
        )
    )


@router.get(
    "/api/v1/users",
    response_model=UserListResponse,
    operation_id="list_users",
)
def list_users(
    _auth: admin_access,
    services: ApiServices = Depends(current_services),
) -> UserListResponse:
    return UserListResponse(data=[user_response(record) for record in services.users.list()])


@router.get(
    "/api/v1/users/{user_id}",
    response_model=UserResponse,
    operation_id="get_user",
)
def get_user(
    user_id: str,
    token: read_token,
    services: ApiServices = Depends(current_services),
) -> UserResponse:
    """Read one account: your own, or any account when you administer the platform."""
    _authorize_user_access(services, token, user_id)
    return user_response(services.users.get(user_id))


@router.post(
    "/api/v1/users/{user_id}/password",
    status_code=status.HTTP_204_NO_CONTENT,
    operation_id="change_user_password",
)
def change_user_password(
    user_id: str,
    request: PasswordChangeRequest,
    token: write_token,
    services: ApiServices = Depends(current_services),
) -> Response:
    """Set a password, ending every session that was minted under the old one.

    Changing your own requires proving you hold the current one, so a borrowed
    session cannot be turned into permanent ownership of the account. An
    administrator reset does not, because that is what a reset is for.
    """
    administrator = _is_platform_administrator(services, token)
    _authorize_user_access(services, token, user_id)
    if not administrator:
        if request.current_password is None:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                "changing your own password requires the current password",
            )
        services.sessions.verify_password(
            user_id=user_id,
            password=request.current_password.get_secret_value(),
        )
    services.users.change_password(user_id, password=request.new_password.get_secret_value())
    services.auth.credentials_revoked()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.put(
    "/api/v1/users/{user_id}/status",
    response_model=UserResponse,
    operation_id="set_user_status",
)
def set_user_status(
    user_id: str,
    request: UserStatusRequest,
    _auth: admin_access,
    services: ApiServices = Depends(current_services),
) -> UserResponse:
    updated = services.users.set_status(user_id, status=request.status)
    services.auth.credentials_revoked()
    return user_response(updated)


@router.get(
    "/api/v1/workspaces/{workspace}/members",
    response_model=WorkspaceMemberListResponse,
    operation_id="list_workspace_members",
)
def list_workspace_members(
    workspace: str,
    principal: read_principal,
    services: ApiServices = Depends(current_services),
) -> WorkspaceMemberListResponse:
    workspace_id = authorize_token_workspace(
        services,
        principal.token,
        workspace,
        AuthScope.Read,
        platform_role=principal.platform_role,
    )
    memberships = services.users.members(workspace_id)
    return WorkspaceMemberListResponse(
        data=[
            _member_response(membership, services.users.get(membership.user_id).username)
            for membership in memberships
        ]
    )


@router.post(
    "/api/v1/workspaces/{workspace}/members",
    response_model=WorkspaceMemberResponse,
    status_code=status.HTTP_201_CREATED,
    operation_id="add_workspace_member",
)
def add_workspace_member(
    workspace: str,
    request: WorkspaceMemberAddRequest,
    principal: write_principal,
    services: ApiServices = Depends(current_services),
) -> WorkspaceMemberResponse:
    """Admitting someone to a workspace is an administrator's decision, not a member's."""
    workspace_id = authorize_token_workspace(
        services,
        principal.token,
        workspace,
        AuthScope.Write,
        platform_role=principal.platform_role,
        required_role=WorkspaceRole.Administrator,
    )
    if request.role is WorkspaceRole.Owner:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "a workspace has exactly one owner; transfer ownership instead of adding one",
        )
    user = services.users.by_username(request.username)
    membership = services.users.add_member(
        workspace_id=workspace_id,
        user_id=user.id,
        role=request.role,
    )
    return _member_response(membership, user.username)


@router.put(
    "/api/v1/workspaces/{workspace}/members/{user_id}",
    response_model=WorkspaceMemberResponse,
    operation_id="set_workspace_member_role",
)
def set_workspace_member_role(
    workspace: str,
    user_id: str,
    request: WorkspaceMemberRoleRequest,
    principal: write_principal,
    services: ApiServices = Depends(current_services),
) -> WorkspaceMemberResponse:
    workspace_id = authorize_token_workspace(
        services,
        principal.token,
        workspace,
        AuthScope.Write,
        platform_role=principal.platform_role,
        required_role=WorkspaceRole.Administrator,
    )
    if request.role is WorkspaceRole.Owner:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "a workspace has exactly one owner; transfer ownership instead",
        )
    membership = services.users.set_member_role(
        workspace_id=workspace_id,
        user_id=user_id,
        role=request.role,
    )
    return _member_response(membership, services.users.get(user_id).username)


@router.delete(
    "/api/v1/workspaces/{workspace}/members/{user_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    operation_id="remove_workspace_member",
)
def remove_workspace_member(
    workspace: str,
    user_id: str,
    principal: write_principal,
    services: ApiServices = Depends(current_services),
) -> Response:
    workspace_id = authorize_token_workspace(
        services,
        principal.token,
        workspace,
        AuthScope.Write,
        platform_role=principal.platform_role,
        required_role=WorkspaceRole.Administrator,
    )
    if not services.users.remove_member(workspace_id=workspace_id, user_id=user_id):
        raise NotFoundError(f"user is not a member of this workspace: {user_id}")
    services.auth.credentials_revoked()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


def _is_platform_administrator(services: ApiServices, token: AuthTokenRecord) -> bool:
    return services.auth.platform_role(token) is PlatformRole.Administrator


def _authorize_user_access(
    services: ApiServices,
    token: AuthTokenRecord,
    user_id: str,
) -> None:
    """Your own account, or any account when you administer the platform."""
    acting_user_id = require_user_principal(token)
    if acting_user_id == user_id:
        return
    if _is_platform_administrator(services, token):
        return
    raise HTTPException(status.HTTP_403_FORBIDDEN, "this account is not yours to read or change")


__all__ = ["router", "user_response"]
