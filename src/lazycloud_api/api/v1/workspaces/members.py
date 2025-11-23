from fastapi import APIRouter, Depends, HTTPException
from loguru import logger

from lazycloud_api.api.dependencies import (
    WorkspaceAccess,
    get_workspace_with_admin_access,
    get_workspace_with_any_access,
    get_workspace_with_owner_access,
)
from lazycloud_api.config import app_config
from lazycloud_api.database import db
from lazycloud_api.database.user_workspaces import WorkspaceRole
from lazycloud_api.services import (
    SubscriptionService,
    get_subscription_service,
)
from lazycloud_api.services.email import email_service
from lazycloud_api.services.subscription_service import SubscriptionLimitError
from shared.requests.workspaces import (
    InviteUserRequest,
    TransferOwnershipRequest,
    UpdateMemberRoleRequest,
)
from shared.responses.workspaces import (
    InviteUserResponse,
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

    members_with_users = await db.user_workspaces.get_workspace_members_with_users(
        workspace.id
    )

    members = [
        WorkspaceMemberResponse(
            user_id=member.user_id,
            name=user.name,
            email=user.email,
            role=member.role,
            status=member.status,
        )
        for member, user in members_with_users
    ]

    pending_invitations = await db.invitations.get_by_workspace(
        workspace.id, include_accepted=False
    )

    for invitation in pending_invitations:
        existing_user = await db.users.get_by_email(invitation.email)
        if not existing_user:
            members.append(
                WorkspaceMemberResponse(
                    user_id=None,
                    name=None,
                    email=invitation.email,
                    role=invitation.role,
                    status="invited",
                )
            )

    return members


@members_router.patch("/{user_id}/role")
async def update_member_role(
    request: UpdateMemberRoleRequest,
    workspace_access: WorkspaceAccess = Depends(get_workspace_with_admin_access),
) -> WorkspaceMemberResponse:
    """Update a member's role (requires owner or admin role)"""
    workspace = workspace_access.workspace

    target_membership = await db.user_workspaces.get_by_user_and_workspace(
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
    updated_membership = await db.user_workspaces.update(target_membership)

    user = await db.users.get_by_id(request.user_id)
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

    target_membership = await db.user_workspaces.get_by_user_and_workspace(
        user_id, workspace.id
    )
    if not target_membership:
        raise HTTPException(status_code=404, detail="User is not a member")

    if target_membership.role == WorkspaceRole.OWNER:
        raise HTTPException(status_code=400, detail="Cannot remove the owner")

    await db.user_workspaces.delete(target_membership.id)

    return WorkspaceSuccessResponse(success=True)


@members_router.post("/invite")
async def invite_user(
    request: InviteUserRequest,
    workspace_access: WorkspaceAccess = Depends(get_workspace_with_admin_access),
) -> InviteUserResponse:
    """Invite a user to a workspace (requires owner or admin role)"""
    workspace = workspace_access.workspace
    user = workspace_access.user

    if workspace.is_personal:
        raise HTTPException(
            status_code=400, detail="Cannot invite users to personal workspaces"
        )

    try:
        token = await db.invitations.create_invitation(
            workspace_id=workspace.id,
            email=request.email,
            role=request.role,
            invited_by_user_id=user.id,
        )

        if request.acceptance_url:
            acceptance_url = request.acceptance_url.replace("{token}", token)
            email_service.send_workspace_invitation(
                email=request.email,
                workspace_name=workspace.name,
                inviter_name=user.name,
                acceptance_url=acceptance_url,
                expiration_days=app_config.INVITATION_EXPIRATION_DAYS,
            )

        logger.info(
            f"Created/resent invitation for email: {request.email} to workspace: {workspace.name} (ID: {workspace.id}) with role: {request.role.value}"
        )
        return InviteUserResponse(success=True, token=token)

    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@members_router.post("/transfer-ownership")
async def transfer_ownership(
    request: TransferOwnershipRequest,
    workspace_access: WorkspaceAccess = Depends(get_workspace_with_owner_access),
    subscription_service: SubscriptionService = Depends(get_subscription_service),
) -> InviteUserResponse:
    """Transfer workspace ownership to another user (requires owner role)"""
    workspace = workspace_access.workspace
    current_owner = workspace_access.user

    if workspace.is_personal:
        raise HTTPException(
            status_code=400, detail="Cannot transfer ownership of personal workspaces"
        )

    # Get new owner user by ID (must exist and be a member)
    new_owner_user = await db.users.get_by_id(request.new_owner_user_id)
    if not new_owner_user:
        raise HTTPException(
            status_code=404,
            detail="User not found.",
        )

    if new_owner_user.id == current_owner.id:
        raise HTTPException(
            status_code=400, detail="Cannot transfer ownership to yourself"
        )

    # Check if new owner is already a member and is an admin (required for ownership transfer)
    new_owner_membership = await db.user_workspaces.get_by_user_and_workspace(
        new_owner_user.id, workspace.id
    )
    if not new_owner_membership:
        raise HTTPException(
            status_code=400,
            detail="The user must already be a workspace member before you can transfer ownership to them. Please invite them as a member first.",
        )

    if new_owner_membership.role != WorkspaceRole.ADMIN:
        raise HTTPException(
            status_code=400,
            detail="Ownership can only be transferred to workspace admins. Please promote the user to admin first.",
        )

    # Get new owner's subscription features
    new_owner_features = await subscription_service.get_user_features(
        new_owner_user.clerk_id
    )

    # Validate workspace can be transferred to new owner
    try:
        await subscription_service.validate_workspace_for_owner(
            workspace.id, new_owner_features, new_owner_user.id
        )

    except SubscriptionLimitError as e:
        raise HTTPException(status_code=e.status_code, detail=str(e))

    try:
        token = await db.invitations.create_invitation(
            workspace_id=workspace.id,
            email=new_owner_user.email,
            role=WorkspaceRole.OWNER,
            invited_by_user_id=current_owner.id,
            invitation_type="ownership_transfer",
        )

        if request.acceptance_url:
            acceptance_url = request.acceptance_url.replace("{token}", token)
            email_service.send_ownership_transfer_invitation(
                email=new_owner_user.email,
                workspace_name=workspace.name,
                current_owner_name=current_owner.name,
                acceptance_url=acceptance_url,
                expiration_days=app_config.INVITATION_EXPIRATION_DAYS,
            )

        logger.info(
            f"Created ownership transfer invitation for user {new_owner_user.id} ({new_owner_user.email}) to workspace: {workspace.name} (ID: {workspace.id})"
        )
        return InviteUserResponse(success=True, token=token)

    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    except HTTPException:
        raise

    except Exception as e:
        logger.error(f"Failed to create ownership transfer invitation: {e}")
        raise HTTPException(
            status_code=500, detail="Failed to create ownership transfer invitation"
        )
