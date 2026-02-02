from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from loguru import logger
from pydantic import BaseModel
from responses.workspaces import WorkspaceSuccessResponse

from backend.api.security import get_current_active_user
from backend.database import Database, get_db
from backend.database.models import (
    InvitationType,
    UserInDb,
    UserWorkspaceStatus,
    WorkspaceInvitationInDb,
)
from backend.services import (
    InvitationService,
    get_invitation_service,
    get_subscription_service,
)
from backend.services.subscription_service import (
    SubscriptionLimitError,
    SubscriptionService,
)

invitations_router = APIRouter(prefix="/invitations", tags=["invitations"])


class InvitationDetailsResponse(BaseModel):
    workspace_id: str
    workspace_name: str
    email: str
    role: str
    invited_by_name: str
    expires_at: str
    invitation_type: str = InvitationType.MEMBER.value
    invitation_id: str  # Required for logged-in users to accept invitations


@invitations_router.get("/pending")
async def get_pending_invitations(
    current_user: UserInDb = Depends(get_current_active_user),
    db: Database = Depends(get_db),
) -> list[InvitationDetailsResponse]:
    """Get all pending invitations for the current user (including ownership transfers)"""
    pending_invitations = await db.invitations.get_pending_by_email(current_user.email)

    result = []
    for invitation in pending_invitations:
        workspace = await db.workspaces.get_by_id(invitation.workspace_id)
        if not workspace:
            continue

        invited_by = await db.users.get_by_id(invitation.invited_by_user_id)
        if not invited_by:
            continue

        result.append(
            InvitationDetailsResponse(
                workspace_id=invitation.workspace_id,
                workspace_name=workspace.name,
                email=invitation.email,
                role=invitation.role,
                invited_by_name=invited_by.name,
                expires_at=invitation.expires_at.isoformat(),
                invitation_type=invitation.invitation_type,
                invitation_id=invitation.id,  # Use invitation_id for accepting
            )
        )

    return result


@invitations_router.post("/{invitation_id}/accept")
async def accept_invitation(
    invitation_id: str,
    current_user: UserInDb = Depends(get_current_active_user),
    subscription_service: SubscriptionService = Depends(get_subscription_service),
    invitation_service: InvitationService = Depends(get_invitation_service),
    db: Database = Depends(get_db),
) -> WorkspaceSuccessResponse:
    """Accept an invitation by ID (for logged-in users)"""
    invitation = await db.invitations.get_by_id(invitation_id)

    if not invitation:
        raise HTTPException(status_code=404, detail="Invitation not found")

    # Verify the invitation belongs to the current user
    if current_user.email.lower().strip() != invitation.email.lower().strip():
        raise HTTPException(
            status_code=403, detail="This invitation does not belong to you"
        )

    # Continue with acceptance logic
    return await _accept_invitation_logic(
        invitation, current_user, subscription_service, invitation_service, db
    )


async def _accept_invitation_logic(
    invitation: WorkspaceInvitationInDb,
    current_user: UserInDb,
    subscription_service: SubscriptionService,
    invitation_service: InvitationService,
    db: Database,
) -> WorkspaceSuccessResponse:
    """Shared logic for accepting invitations"""

    if invitation.accepted_at:
        raise HTTPException(
            status_code=400, detail="Invitation has already been accepted"
        )

    if invitation.expires_at < datetime.now(timezone.utc):
        raise HTTPException(status_code=400, detail="Invitation has expired")

    # Handle ownership transfer differently
    if invitation.invitation_type == InvitationType.OWNERSHIP_TRANSFER.value:
        # Re-validate workspace for new owner (in case plan changed since invitation)
        new_owner_features = await subscription_service.get_user_features(
            current_user.workos_id
        )
        try:
            await subscription_service.validate_workspace_for_owner(
                invitation.workspace_id, new_owner_features, current_user.id
            )

        except SubscriptionLimitError as e:
            raise HTTPException(status_code=e.status_code, detail=str(e))

        current_owner_membership = await db.workspaces.get_owner(
            invitation.workspace_id
        )
        if not current_owner_membership:
            raise HTTPException(status_code=400, detail="Workspace owner not found")

        # Perform ownership transfer and mark invitation as accepted in a single transaction
        await db.workspaces.transfer_ownership(
            invitation.workspace_id,
            current_owner_membership.user_id,
            current_user.id,
        )

        await db.invitations.accept_invitation(invitation.id)

        logger.info(
            f"User {current_user.id} accepted ownership transfer for workspace {invitation.workspace_id}"
        )

        return WorkspaceSuccessResponse(success=True)

    else:
        # Regular member invitation
        try:
            workspace_id, role = await invitation_service.accept_invitation(
                invitation.id, current_user.id
            )

            logger.info(
                f"User {current_user.id} accepted invitation to workspace {workspace_id} with role {role}"
            )

            return WorkspaceSuccessResponse(success=True)

        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))


@invitations_router.post("/{invitation_id}/decline")
async def decline_invitation(
    invitation_id: str,
    current_user: UserInDb = Depends(get_current_active_user),
    db: Database = Depends(get_db),
) -> WorkspaceSuccessResponse:
    """Decline an invitation by ID (for logged-in users)"""
    invitation = await db.invitations.get_by_id(invitation_id)

    if not invitation:
        raise HTTPException(status_code=404, detail="Invitation not found")

    # Verify the invitation belongs to the current user
    if current_user.email.lower().strip() != invitation.email.lower().strip():
        raise HTTPException(
            status_code=403, detail="This invitation does not belong to you"
        )

    # Check if already accepted
    if invitation.accepted_at:
        raise HTTPException(
            status_code=400, detail="Invitation has already been accepted"
        )

    # Handle ownership transfer invitations differently
    if invitation.invitation_type == InvitationType.OWNERSHIP_TRANSFER.value:
        # Ownership transfer invitations don't create INVITED user_workspace records
        # The user is already an active member, so just delete the invitation
        await db.invitations.delete(invitation.id)

        logger.info(
            f"User {current_user.id} declined ownership transfer invitation {invitation_id} for workspace {invitation.workspace_id}"
        )
        return WorkspaceSuccessResponse(success=True)

    # Handle regular member invitations
    # Delete invitation using repository method
    await db.invitations.delete(invitation.id)

    # Also delete the user_workspace record if it exists with INVITED status
    existing_user = await db.users.get_by_email(invitation.email)
    if existing_user:
        membership = await db.user_workspaces.get_by_user_and_workspace(
            existing_user.id, invitation.workspace_id
        )
        if membership and membership.status == UserWorkspaceStatus.INVITED:
            await db.user_workspaces.delete(membership.id)

    logger.info(
        f"User {current_user.id} declined invitation {invitation_id} for workspace {invitation.workspace_id}"
    )

    return WorkspaceSuccessResponse(success=True)
