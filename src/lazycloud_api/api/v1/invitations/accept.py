from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from loguru import logger
from pydantic import BaseModel

from lazycloud_api.api.security import get_current_active_user
from lazycloud_api.database import db
from lazycloud_api.database.invitations import WorkspaceInvitationService
from lazycloud_api.database.users import UserPydantic
from lazycloud_api.services import (
    get_invitation_service,
    get_subscription_service,
)
from lazycloud_api.services.subscription_service import (
    SubscriptionLimitError,
    SubscriptionService,
)
from shared.responses.workspaces import WorkspaceSuccessResponse

invitations_router = APIRouter(prefix="/invitations", tags=["invitations"])


class InvitationDetailsResponse(BaseModel):
    workspace_id: str
    workspace_name: str
    email: str
    role: str
    invited_by_name: str
    expires_at: str
    invitation_type: str = "member"


@invitations_router.get("/{token}")
async def get_invitation(
    token: str,
    invitation_service: WorkspaceInvitationService = Depends(get_invitation_service),
) -> InvitationDetailsResponse:
    """Get invitation details by token"""
    invitation = await invitation_service.get_by_token(token)

    if not invitation:
        raise HTTPException(status_code=404, detail="Invitation not found")

    if invitation.accepted_at:
        raise HTTPException(
            status_code=400, detail="Invitation has already been accepted"
        )

    if invitation.expires_at < datetime.now(timezone.utc):
        raise HTTPException(status_code=400, detail="Invitation has expired")

    workspace = await db.workspaces.get_by_id(invitation.workspace_id)
    if not workspace:
        raise HTTPException(status_code=404, detail="Workspace not found")

    invited_by = await db.users.get_by_id(invitation.invited_by_user_id)
    if not invited_by:
        raise HTTPException(status_code=404, detail="Inviter not found")

    return InvitationDetailsResponse(
        workspace_id=invitation.workspace_id,
        workspace_name=workspace.name,
        email=invitation.email,
        role=invitation.role,
        invited_by_name=invited_by.name,
        expires_at=invitation.expires_at.isoformat(),
        invitation_type=invitation.invitation_type,
    )


@invitations_router.post("/{token}/accept")
async def accept_invitation(
    token: str,
    current_user: UserPydantic = Depends(get_current_active_user),
    subscription_service: SubscriptionService = Depends(get_subscription_service),
) -> WorkspaceSuccessResponse:
    """Accept an invitation"""
    invitation = await db.invitations.get_by_token(token)

    if not invitation:
        raise HTTPException(status_code=404, detail="Invitation not found")

    if invitation.accepted_at:
        raise HTTPException(
            status_code=400, detail="Invitation has already been accepted"
        )

    if invitation.expires_at < datetime.now(timezone.utc):
        raise HTTPException(status_code=400, detail="Invitation has expired")

    if current_user.email.lower().strip() != invitation.email.lower().strip():
        raise HTTPException(
            status_code=400, detail="Invitation email does not match your email"
        )

    # Handle ownership transfer differently
    if invitation.invitation_type == "ownership_transfer":
        # Re-validate workspace for new owner (in case plan changed since invitation)
        new_owner_features = await subscription_service.get_user_features(
            current_user.clerk_id
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
        async with db.invitations.transaction() as session:
            await db.workspaces.transfer_ownership(
                invitation.workspace_id,
                current_owner_membership.user_id,
                current_user.id,
                session=session,
            )

            await db.invitations.accept_invitation(invitation.id, session=session)

        logger.info(
            f"User {current_user.id} accepted ownership transfer for workspace {invitation.workspace_id}"
        )

        return WorkspaceSuccessResponse(success=True)

    else:
        # Regular member invitation
        try:
            workspace_id, role = await db.invitations.accept_invitation(
                token, current_user.id
            )
            logger.info(
                f"User {current_user.id} accepted invitation to workspace {workspace_id} with role {role}"
            )
            return WorkspaceSuccessResponse(success=True)

        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
