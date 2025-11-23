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
from lazycloud_api.database.user_workspaces import (
    UserWorkspaceStatus,
    WorkspaceRole,
)
from lazycloud_api.services import (
    InvitationService,
    SubscriptionService,
    get_invitation_service,
    get_subscription_service,
)
from lazycloud_api.services.email import email_service
from lazycloud_api.services.subscription_service import SubscriptionLimitError
from shared.models.workspaces import InvitationType
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

    # Filter out ownership transfer invitations - they should only show in transfer dialog
    member_invitations = [
        inv
        for inv in pending_invitations
        if inv.invitation_type != InvitationType.OWNERSHIP_TRANSFER.value
    ]

    # Get all existing member user IDs to check for duplicates
    existing_member_user_ids = {member.user_id for member in members if member.user_id}

    for invitation in member_invitations:
        existing_user = await db.users.get_by_email(invitation.email)

        # Only add invitation if:
        # 1. User doesn't exist, OR
        # 2. User exists but is not currently a member of this workspace
        if not existing_user:
            # User doesn't exist - add invitation
            members.append(
                WorkspaceMemberResponse(
                    user_id=None,
                    name=None,
                    email=invitation.email,
                    role=invitation.role,
                    status="invited",
                    invitation_id=invitation.id,
                )
            )
        elif existing_user.id not in existing_member_user_ids:
            # User exists but is not a member - add invitation with user info
            members.append(
                WorkspaceMemberResponse(
                    user_id=existing_user.id,
                    name=existing_user.name,
                    email=invitation.email,
                    role=invitation.role,
                    status="invited",
                    invitation_id=invitation.id,
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

    # Get user's email before deleting membership
    target_user = await db.users.get_by_id(user_id)

    # Wrap invitation deletions and membership deletion in a transaction
    async with db.user_workspaces.transaction() as session:
        if target_user:
            # Cancel any pending invitations for this user in this workspace
            pending_invitations = await db.invitations.get_by_workspace(
                workspace.id, include_accepted=False
            )
            for invitation in pending_invitations:
                if (
                    invitation.email.lower().strip()
                    == target_user.email.lower().strip()
                    and not invitation.accepted_at
                ):
                    # Delete invitation using repository method with session
                    await db.invitations.delete(invitation.id, session=session)
                    logger.info(
                        f"Cancelled pending invitation for {target_user.email} in workspace {workspace.name} (ID: {workspace.id}) when removing member"
                    )

        # Delete membership using repository method with session
        await db.user_workspaces.delete(target_membership.id, session=session)

    logger.info(
        f"Removed member {user_id} from workspace {workspace.name} (ID: {workspace.id})"
    )

    return WorkspaceSuccessResponse(success=True)


@members_router.post("/invite")
async def invite_user(
    request: InviteUserRequest,
    workspace_access: WorkspaceAccess = Depends(get_workspace_with_admin_access),
    invitation_service: InvitationService = Depends(get_invitation_service),
) -> InviteUserResponse:
    """Invite a user to a workspace (requires owner or admin role)"""
    workspace = workspace_access.workspace
    user = workspace_access.user

    if workspace.is_personal:
        raise HTTPException(
            status_code=400, detail="Cannot invite users to personal workspaces"
        )

    try:
        token = await invitation_service.create_or_resend_invitation(
            workspace_id=workspace.id,
            email=request.email,
            role=request.role,
            invited_by_user_id=user.id,
        )

        if request.acceptance_url:
            workspaces_url = request.acceptance_url
            try:
                email_service.send_workspace_invitation(
                    email=request.email,
                    workspace_name=workspace.name,
                    inviter_name=user.name,
                    workspaces_url=workspaces_url,
                    expiration_days=app_config.INVITATION_EXPIRATION_DAYS,
                )

            except Exception as e:
                logger.warning(
                    f"Failed to send invitation email to {request.email}: {e}. "
                    f"Invitation was created successfully with token: {token}"
                )

        logger.info(
            f"Created/resent invitation for email: {request.email} to workspace: {workspace.name} (ID: {workspace.id}) with role: {request.role.value}"
        )
        return InviteUserResponse(success=True, token=token)

    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@members_router.delete("/invitations/{invitation_id}")
async def cancel_invitation(
    invitation_id: str,
    workspace_access: WorkspaceAccess = Depends(get_workspace_with_admin_access),
) -> WorkspaceSuccessResponse:
    """Cancel a pending invitation by invitation ID (requires owner or admin role)"""
    workspace = workspace_access.workspace

    invitation = await db.invitations.get_by_id(invitation_id)

    if not invitation:
        raise HTTPException(status_code=404, detail="Invitation not found")

    # Verify the invitation belongs to this workspace
    if invitation.workspace_id != workspace.id:
        raise HTTPException(
            status_code=403, detail="Invitation does not belong to this workspace"
        )

    # Handle ownership transfer invitations
    if invitation.invitation_type == InvitationType.OWNERSHIP_TRANSFER.value:
        if invitation.accepted_at:
            raise HTTPException(
                status_code=400,
                detail="Cannot cancel an accepted ownership transfer invitation",
            )
        # Ownership transfer is pending, allow cancellation
        async with db.invitations.transaction() as session:
            await db.invitations.delete(invitation.id, session=session)
        logger.info(
            f"Cancelled ownership transfer invitation {invitation_id} in workspace {workspace.name} (ID: {workspace.id})"
        )
        return WorkspaceSuccessResponse(success=True)

    # Handle regular member invitations
    # Check if user is currently an active member
    # If invitation was accepted but user is no longer a member, allow canceling
    existing_user = await db.users.get_by_email(invitation.email)
    is_currently_member = False
    if existing_user:
        membership = await db.user_workspaces.get_by_user_and_workspace(
            existing_user.id, workspace.id
        )
        is_currently_member = (
            membership is not None and membership.status == UserWorkspaceStatus.ACTIVE
        )

    # Only prevent canceling if invitation was accepted AND user is still an active member
    if invitation.accepted_at and is_currently_member:
        raise HTTPException(
            status_code=400,
            detail="Cannot cancel an accepted invitation for an active member",
        )

    # Wrap invitation deletion and user_workspace deletion in a transaction
    async with db.invitations.transaction() as session:
        # Delete invitation using repository method
        await db.invitations.delete(invitation.id, session=session)

        # Also delete the user_workspace record if it exists with INVITED status
        if existing_user:
            membership = await db.user_workspaces.get_by_user_and_workspace(
                existing_user.id, workspace.id, session=session
            )
            if membership and membership.status == UserWorkspaceStatus.INVITED:
                await db.user_workspaces.delete(membership.id, session=session)

    logger.info(
        f"Cancelled invitation {invitation_id} for {invitation.email} in workspace {workspace.name} (ID: {workspace.id})"
    )

    return WorkspaceSuccessResponse(success=True)


@members_router.get("/transfer-ownership/pending")
async def get_pending_ownership_transfer(
    workspace_access: WorkspaceAccess = Depends(get_workspace_with_owner_access),
) -> WorkspaceMemberResponse | None:
    """Get pending ownership transfer invitation for a workspace (owner only)"""
    workspace = workspace_access.workspace

    pending_invitations = await db.invitations.get_by_workspace(
        workspace.id, include_accepted=False
    )

    ownership_transfer = next(
        (
            inv
            for inv in pending_invitations
            if inv.invitation_type == InvitationType.OWNERSHIP_TRANSFER.value
            and not inv.accepted_at
        ),
        None,
    )

    if not ownership_transfer:
        return None

    existing_user = await db.users.get_by_email(ownership_transfer.email)
    if not existing_user:
        return WorkspaceMemberResponse(
            user_id=None,
            name=None,
            email=ownership_transfer.email,
            role=ownership_transfer.role,
            status="invited",
            invitation_id=ownership_transfer.id,
        )

    return WorkspaceMemberResponse(
        user_id=existing_user.id,
        name=existing_user.name,
        email=ownership_transfer.email,
        role=ownership_transfer.role,
        status="invited",
        invitation_id=ownership_transfer.id,
    )


@members_router.post("/transfer-ownership")
async def transfer_ownership(
    request: TransferOwnershipRequest,
    workspace_access: WorkspaceAccess = Depends(get_workspace_with_owner_access),
    subscription_service: SubscriptionService = Depends(get_subscription_service),
    invitation_service: InvitationService = Depends(get_invitation_service),
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
        token = await invitation_service.create_or_resend_invitation(
            workspace_id=workspace.id,
            email=new_owner_user.email,
            role=WorkspaceRole.OWNER,
            invited_by_user_id=current_owner.id,
            invitation_type=InvitationType.OWNERSHIP_TRANSFER,
        )

        if request.acceptance_url:
            # acceptance_url is already the workspaces URL (e.g., /workspaces)
            workspaces_url = request.acceptance_url
            try:
                email_service.send_ownership_transfer_invitation(
                    email=new_owner_user.email,
                    workspace_name=workspace.name,
                    current_owner_name=current_owner.name,
                    workspaces_url=workspaces_url,
                    expiration_days=app_config.INVITATION_EXPIRATION_DAYS,
                )
            except Exception as e:
                logger.warning(
                    f"Failed to send ownership transfer email to {new_owner_user.email}: {e}. "
                    f"Invitation was created successfully with token: {token}"
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
