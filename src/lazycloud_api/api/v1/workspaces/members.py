from fastapi import APIRouter, Depends, HTTPException
from loguru import logger

from lazycloud_api.api.dependencies import (
    WorkspaceAccess,
    get_workspace_with_admin_access,
    get_workspace_with_any_access,
)
from lazycloud_api.database import db
from lazycloud_api.database.user_workspaces import WorkspaceRole
from shared.requests.workspaces import InviteUserRequest, UpdateMemberRoleRequest
from shared.responses.workspaces import (
    WorkspaceMemberResponse,
    WorkspaceSuccessResponse,
)

members_router = APIRouter(prefix="/{workspace_id}/members", tags=["workspace-members"])


@members_router.get("")
async def get_workspace_members(
    workspace_access: WorkspaceAccess = Depends(get_workspace_with_any_access),
) -> list[WorkspaceMemberResponse]:
    """Get all members of a workspace"""
    workspace = workspace_access.workspace

    members_with_users = await db.user_workspaces.aget_workspace_members_with_users(
        workspace.id
    )

    return [
        WorkspaceMemberResponse(
            user_id=member.user_id,
            name=user.name,
            email=user.email,
            role=member.role,
            status=member.status,
        )
        for member, user in members_with_users
    ]


@members_router.patch("/{user_id}/role")
async def update_member_role(
    request: UpdateMemberRoleRequest,
    workspace_access: WorkspaceAccess = Depends(get_workspace_with_admin_access),
) -> WorkspaceMemberResponse:
    """Update a member's role (requires owner or admin role)"""
    workspace = workspace_access.workspace

    target_membership = await db.user_workspaces.aget_by_user_and_workspace(
        request.user_id, workspace.id
    )
    if not target_membership:
        raise HTTPException(status_code=404, detail="User is not a member")

    if target_membership.role == WorkspaceRole.OWNER:
        raise HTTPException(
            status_code=400,
            detail="Cannot change owner role. Use transfer ownership instead.",
        )

    if request.role == WorkspaceRole.OWNER:
        raise HTTPException(
            status_code=400,
            detail="Cannot set role to owner. Use transfer ownership instead.",
        )

    target_membership.role = request.role
    updated_membership = await db.user_workspaces.aupdate(target_membership)

    user = await db.users.aget_by_id(request.user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    return WorkspaceMemberResponse(
        user_id=updated_membership.user_id,
        name=user.name,
        email=user.email,
        role=updated_membership.role,
        status=updated_membership.status,
    )


@members_router.delete("/{user_id}")
async def remove_member(
    user_id: str,
    workspace_access: WorkspaceAccess = Depends(get_workspace_with_admin_access),
) -> WorkspaceSuccessResponse:
    """Remove a member from a workspace (requires owner or admin role)"""
    workspace = workspace_access.workspace

    target_membership = await db.user_workspaces.aget_by_user_and_workspace(
        user_id, workspace.id
    )
    if not target_membership:
        raise HTTPException(status_code=404, detail="User is not a member")

    if target_membership.role == WorkspaceRole.OWNER:
        raise HTTPException(status_code=400, detail="Cannot remove the owner")

    await db.user_workspaces.adelete(target_membership.id)

    return WorkspaceSuccessResponse(success=True)


@members_router.post("/invite")
async def invite_user(
    request: InviteUserRequest,
    workspace_access: WorkspaceAccess = Depends(get_workspace_with_admin_access),
) -> WorkspaceSuccessResponse:
    """Invite a user to a workspace (requires owner or admin role)"""
    workspace = workspace_access.workspace

    if workspace.is_personal:
        raise HTTPException(
            status_code=400, detail="Cannot invite users to personal workspaces"
        )

    # TODO: Send email to user with invite link
    logger.info(
        f"Inviting user with email: {request.email} to workspace: {workspace.name} (ID: {workspace.id}) with role: {request.role.value}"
    )

    return WorkspaceSuccessResponse(success=True)
