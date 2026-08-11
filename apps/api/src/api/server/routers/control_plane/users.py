from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Response, status
from shared.errors import NotFoundError
from shared.http.users import (
    UserCreateRequest,
    UserListResponse,
    UserResponse,
    UserRoleRequest,
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
    UserIdentityRecord,
    UserRecord,
    WorkspaceMemberRecord,
    WorkspaceRole,
)

from api.server.auth import (
    admin_access,
    read_principal,
    read_token,
    write_principal,
)
from api.server.dependencies import (
    authorize_token_workspace,
    current_services,
    require_user_principal,
)
from api.server.services import ApiServices

router = APIRouter()


def user_response(
    record: UserRecord,
    identity: UserIdentityRecord | None = None,
) -> UserResponse:
    """Project a user for the API.

    An account with no identity reports empty GitHub fields rather than being hidden:
    it is a real account that owns tokens, and an operator listing accounts needs to
    see that this one has no way to sign in.
    """
    return UserResponse(
        id=record.id,
        display_name=record.display_name,
        email=record.email,
        avatar_url=record.avatar_url,
        github_user_id=identity.subject if identity is not None else "",
        github_login=identity.subject_login if identity is not None else "",
        role=record.role,
        status=record.status,
        created_at=record.created_at,
        updated_at=record.updated_at,
    )


def _member_response(
    user: UserRecord,
    membership: WorkspaceMemberRecord,
) -> WorkspaceMemberResponse:
    return WorkspaceMemberResponse(
        user_id=membership.user_id,
        display_name=user.display_name,
        email=user.email,
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
    """Create an account, optionally pre-linked to the GitHub identity that reaches it.

    Without the link the account cannot sign in and exists only to own tokens. With
    it, that person's first sign-in lands here instead of opening a second account
    with none of the standing this one was given.
    """
    created = services.users.create(
        display_name=request.display_name,
        github_user_id=request.github_user_id,
        github_login=request.github_login,
        role=request.role,
    )
    return user_response(created, services.users.identity(created.id))


@router.get(
    "/api/v1/users",
    response_model=UserListResponse,
    operation_id="list_users",
)
def list_users(
    _auth: admin_access,
    services: ApiServices = Depends(current_services),
) -> UserListResponse:
    records = services.users.list()
    identities = services.users.identities([record.id for record in records])
    return UserListResponse(
        data=[user_response(record, identities.get(record.id)) for record in records]
    )


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
    return user_response(services.users.get(user_id), services.users.identity(user_id))


@router.put(
    "/api/v1/users/{user_id}/role",
    response_model=UserResponse,
    operation_id="set_user_role",
)
def set_user_role(
    user_id: str,
    request: UserRoleRequest,
    _auth: admin_access,
    services: ApiServices = Depends(current_services),
) -> UserResponse:
    """Grant or withdraw platform administrator standing.

    Signing in makes an ordinary member, so this is what an administrator uses to
    promote somebody who already has an account. Caches are dropped afterwards
    because the role decides authorization and a replica holding the old answer
    would keep applying it.
    """
    updated = services.users.set_role(user_id, role=request.role)
    services.auth.credentials_revoked()
    return user_response(updated, services.users.identity(user_id))


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
    return user_response(updated, services.users.identity(user_id))


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
            _member_response(services.users.get(membership.user_id), membership)
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
    user = services.users.get(request.user_id)
    membership = services.users.add_member(
        workspace_id=workspace_id,
        user_id=user.id,
        role=request.role,
    )
    return _member_response(user, membership)


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
    return _member_response(services.users.get(user_id), membership)


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
