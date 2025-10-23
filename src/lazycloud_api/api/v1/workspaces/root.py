from fastapi import APIRouter, Depends, HTTPException

from lazycloud_api.api.dependencies import (
    get_workspace_with_admin_access,
    get_workspace_with_any_access,
)
from lazycloud_api.api.security import get_current_active_user
from lazycloud_api.database import db
from lazycloud_api.database.user_workspaces import (
    UserWorkspacePydantic,
    UserWorkspaceStatus,
    WorkspaceRole,
)
from lazycloud_api.database.users import UserPydantic
from lazycloud_api.database.utils import validate_workspace_name
from lazycloud_api.database.workspaces import WorkspacePydantic, WorkspaceStatus
from shared.requests.workspaces import (
    CreateWorkspaceRequest,
    InviteUserRequest,
    RenameWorkspaceRequest,
    UpdateMemberRoleRequest,
)
from shared.responses.workspaces import (
    WorkspaceMemberResponse,
    WorkspaceResponse,
    WorkspaceSuccessResponse,
)

workspaces_router = APIRouter(prefix="/workspaces", tags=["workspaces"])


@workspaces_router.get("")
async def get_workspaces(
    current_user: UserPydantic = Depends(get_current_active_user),
) -> list[WorkspaceResponse]:
    """Get all workspaces the current user has access to"""
    user_workspaces = await db.workspaces.aget_user_workspaces_with_membership(
        current_user.id
    )

    return [
        WorkspaceResponse(
            id=workspace.id,
            name=workspace.name,
            is_personal=workspace.is_personal,
            role=membership.role,
        )
        for workspace, membership in user_workspaces
    ]


@workspaces_router.get("/{workspace_id}")
async def get_workspace(
    workspace_membership: tuple[UserWorkspacePydantic, WorkspacePydantic] = Depends(
        get_workspace_with_any_access
    ),
) -> WorkspaceResponse:
    """Get a specific workspace by ID"""
    membership, workspace = workspace_membership

    return WorkspaceResponse(
        id=workspace.id,
        name=workspace.name,
        is_personal=workspace.is_personal,
        role=membership.role,
    )


@workspaces_router.post("")
async def create_workspace(
    request: CreateWorkspaceRequest,
    current_user: UserPydantic = Depends(get_current_active_user),
) -> WorkspaceResponse:
    """Create a new workspace"""
    try:
        validated_name = validate_workspace_name(request.name)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    workspace = WorkspacePydantic(
        name=validated_name,
        is_personal=False,
    )
    workspace = await db.workspaces.acreate(workspace)

    try:
        # Add current user as owner
        membership = UserWorkspacePydantic(
            user_id=current_user.id,
            workspace_id=workspace.id,
            role=WorkspaceRole.OWNER,
            status=UserWorkspaceStatus.ACTIVE,
        )
        membership = await db.user_workspaces.acreate(membership)
    except Exception:
        # If membership creation fails, delete the orphaned workspace
        await db.workspaces.adelete(workspace.id)
        raise HTTPException(status_code=500, detail="Failed to create workspace")

    return WorkspaceResponse(
        id=workspace.id,
        name=workspace.name,
        is_personal=workspace.is_personal,
        role=membership.role,
    )


@workspaces_router.patch("/{workspace_id}/rename")
async def rename_workspace(
    request: RenameWorkspaceRequest,
    workspace_membership: tuple[UserWorkspacePydantic, WorkspacePydantic] = Depends(
        get_workspace_with_admin_access
    ),
) -> WorkspaceResponse:
    """Rename a workspace (requires owner role)"""
    try:
        validated_name = validate_workspace_name(request.name)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    membership, workspace = workspace_membership

    workspace.name = validated_name
    workspace = await db.workspaces.aupdate(workspace)

    return WorkspaceResponse(
        id=workspace.id,
        name=workspace.name,
        is_personal=workspace.is_personal,
        role=membership.role,
    )


@workspaces_router.post("/{workspace_id}/invite")
async def invite_user(
    request: InviteUserRequest,
    workspace_membership: tuple[UserWorkspacePydantic, WorkspacePydantic] = Depends(
        get_workspace_with_admin_access
    ),
) -> WorkspaceMemberResponse:
    """Invite a user to a workspace (requires owner or admin role)"""
    # Check if current user has permission
    _, workspace = workspace_membership

    if workspace.is_personal:
        raise HTTPException(
            status_code=400, detail="Cannot invite users to personal workspaces"
        )

    # Check if user exists
    user = await db.users.aget_by_id(request.user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    # Check if user is already a member
    existing_membership = await db.user_workspaces.aget_by_user_and_workspace(
        request.user_id, workspace.id
    )
    if existing_membership:
        raise HTTPException(status_code=400, detail="User is already a member")

    # make sure requested is not an admin or owner
    if request.role in [WorkspaceRole.ADMIN, WorkspaceRole.OWNER]:
        raise HTTPException(status_code=400, detail="Cannot invite admin or owner")

    # Create invitation
    new_membership = UserWorkspacePydantic(
        user_id=request.user_id,
        workspace_id=workspace.id,
        role=request.role,
        status=UserWorkspaceStatus.INVITED,
    )
    new_membership = await db.user_workspaces.acreate(new_membership)

    return WorkspaceMemberResponse(
        user_id=new_membership.user_id,
        role=new_membership.role,
        status=new_membership.status,
    )


@workspaces_router.get("/{workspace_id}/members")
async def get_workspace_members(
    workspace_membership: tuple[UserWorkspacePydantic, WorkspacePydantic] = Depends(
        get_workspace_with_any_access
    ),
) -> list[WorkspaceMemberResponse]:
    """Get all members of a workspace"""
    # Check if user has access
    _, workspace = workspace_membership

    # Get all members
    members = await db.user_workspaces.aget_workspace_members(workspace.id)

    return [
        WorkspaceMemberResponse(
            user_id=member.user_id,
            role=member.role,
            status=member.status,
        )
        for member in members
    ]


@workspaces_router.patch("/{workspace_id}/members/{user_id}/role")
async def update_member_role(
    request: UpdateMemberRoleRequest,
    workspace_membership: tuple[UserWorkspacePydantic, WorkspacePydantic] = Depends(
        get_workspace_with_admin_access
    ),
) -> WorkspaceMemberResponse:
    """Update a member's role (requires owner or admin role)"""
    # Check if current user has permission
    _, workspace = workspace_membership

    # Get target member
    target_membership = await db.user_workspaces.aget_by_user_and_workspace(
        request.user_id, workspace.id
    )
    if not target_membership:
        raise HTTPException(status_code=404, detail="User is not a member")

    # Prevent changing owner role (can only be changed via transfer)
    if target_membership.role == WorkspaceRole.OWNER:
        raise HTTPException(
            status_code=400,
            detail="Cannot change owner role. Use transfer ownership instead.",
        )

    # Prevent setting role to owner (can only be done via transfer)
    if request.role == WorkspaceRole.OWNER:
        raise HTTPException(
            status_code=400,
            detail="Cannot set role to owner. Use transfer ownership instead.",
        )

    # Update the role
    target_membership.role = request.role
    updated_membership = await db.user_workspaces.aupdate(target_membership)

    return WorkspaceMemberResponse(
        user_id=updated_membership.user_id,
        role=updated_membership.role,
        status=updated_membership.status,
    )


@workspaces_router.delete("/{workspace_id}/members/{user_id}")
async def remove_member(
    user_id: str,
    workspace_membership: tuple[UserWorkspacePydantic, WorkspacePydantic] = Depends(
        get_workspace_with_admin_access
    ),
) -> WorkspaceSuccessResponse:
    """Remove a member from a workspace (requires owner or admin role)"""
    # Check if current user has permission
    _, workspace = workspace_membership

    # Check if target user is a member
    target_membership = await db.user_workspaces.aget_by_user_and_workspace(
        user_id, workspace.id
    )
    if not target_membership:
        raise HTTPException(status_code=404, detail="User is not a member")

    # Prevent removing the owner
    if target_membership.role == WorkspaceRole.OWNER:
        raise HTTPException(status_code=400, detail="Cannot remove the owner")

    # Remove the member
    await db.user_workspaces.adelete(target_membership.id)

    return WorkspaceSuccessResponse(success=True)


@workspaces_router.delete("/{workspace_id}")
async def delete_workspace(
    workspace_membership: tuple[UserWorkspacePydantic, WorkspacePydantic] = Depends(
        get_workspace_with_admin_access
    ),
) -> WorkspaceSuccessResponse:
    """Delete a workspace (requires owner role)"""
    # Check if current user is the owner
    _, workspace = workspace_membership

    # Prevent deleting personal workspaces
    if workspace.is_personal:
        raise HTTPException(status_code=400, detail="Cannot delete personal workspace")

    # get all deployments in the workspace
    deployments = await db.compose_deployments.afind({"workspace_id": workspace.id})
    if len(deployments) > 0:
        # delete all deployments for the workspace
        await db.compose_deployments.adelete_bulk(
            [deployment.id for deployment in deployments]
        )

    # set workspace status to deleted
    workspace = await db.workspaces.aupdate_status(
        workspace.id, WorkspaceStatus.DELETED
    )

    return WorkspaceSuccessResponse(success=True)
