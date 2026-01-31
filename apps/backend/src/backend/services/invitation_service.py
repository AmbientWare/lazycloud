import secrets
from datetime import datetime, timedelta, timezone

from backend.config import app_config
from backend.database import get_db_context
from backend.database.models import (
    InvitationType,
    UserWorkspace,
    UserWorkspaceStatus,
    WorkspaceRole,
)


class InvitationService:
    """Service for managing workspace invitations."""

    def _generate_token(self) -> str:
        """Generate a cryptographically secure token"""
        return secrets.token_urlsafe(32)

    def _calculate_expires_at(self) -> datetime:
        """Calculate expiration date for invitation"""
        expiration_days = app_config.INVITATION_EXPIRATION_DAYS
        return datetime.now(timezone.utc) + timedelta(days=expiration_days)

    async def create_or_resend_invitation(
        self,
        workspace_id: str,
        email: str,
        role: WorkspaceRole,
        invited_by_user_id: str,
        invitation_type: InvitationType = InvitationType.MEMBER,
    ) -> str:
        """Create a new invitation or resend an existing one, return the token."""
        email = email.lower().strip()

        async with get_db_context() as db:
            existing_user = await db.users.get_by_email(email)

            # For ownership transfers, only check for existing ownership transfer invitations
            if invitation_type == InvitationType.OWNERSHIP_TRANSFER:
                workspace_invitations = await db.invitations.get_by_workspace(
                    workspace_id, include_accepted=False
                )
                existing_ownership_transfer = next(
                    (
                        inv
                        for inv in workspace_invitations
                        if inv.invitation_type
                        == InvitationType.OWNERSHIP_TRANSFER.value
                        and not inv.accepted_at
                    ),
                    None,
                )

                if existing_ownership_transfer:
                    raise ValueError(
                        "An ownership transfer invitation is already pending. "
                        "Please cancel the existing invitation before sending a new one."
                    )

                existing_invitations = await db.invitations.find(
                    {
                        "workspace_id": workspace_id,
                        "email": email.lower().strip(),
                        "invitation_type": InvitationType.OWNERSHIP_TRANSFER.value,
                    }
                )
                existing_pending_invitations = [
                    inv for inv in existing_invitations if not inv.accepted_at
                ]

            else:
                # For regular member invitations, check if user is already an active member
                is_currently_member = False
                if existing_user:
                    current_membership = (
                        await db.user_workspaces.get_by_user_and_workspace(
                            existing_user.id, workspace_id
                        )
                    )
                    is_currently_member = (
                        current_membership is not None
                        and current_membership.status == UserWorkspaceStatus.ACTIVE
                    )

                if is_currently_member:
                    raise ValueError(
                        "User is already an active member of this workspace"
                    )

                existing_invitations = await db.invitations.find(
                    {
                        "workspace_id": workspace_id,
                        "email": email.lower().strip(),
                        "invitation_type": InvitationType.MEMBER.value,
                    }
                )
                existing_pending_invitations = [
                    inv for inv in existing_invitations if not inv.accepted_at
                ]

            # Delete ALL pending invitations of this type to prevent duplicates
            for pending_invitation in existing_pending_invitations:
                await db.invitations.delete(pending_invitation.id)

            # Clean up any INVITED status user_workspace record if we deleted invitations
            if existing_pending_invitations and existing_user:
                existing_membership = (
                    await db.user_workspaces.get_by_user_and_workspace(
                        existing_user.id, workspace_id
                    )
                )
                if (
                    existing_membership
                    and existing_membership.status == UserWorkspaceStatus.INVITED
                ):
                    await db.user_workspaces.delete(existing_membership.id)

            # Create a new invitation with a fresh token
            token = self._generate_token()
            expires_at = self._calculate_expires_at()

            await db.invitations.create_invitation(
                workspace_id=workspace_id,
                email=email,
                role=role,
                invited_by_user_id=invited_by_user_id,
                token=token,
                expires_at=expires_at,
                invitation_type=invitation_type.value,
            )

            # Create or update user_workspace record with INVITED status
            if invitation_type != InvitationType.OWNERSHIP_TRANSFER and existing_user:
                existing_membership = (
                    await db.user_workspaces.get_by_user_and_workspace(
                        existing_user.id, workspace_id
                    )
                )

                if not existing_membership:
                    membership = UserWorkspace(
                        user_id=existing_user.id,
                        workspace_id=workspace_id,
                        role=role,
                        status=UserWorkspaceStatus.INVITED,
                    )
                    await db.user_workspaces.create(membership)
                elif existing_membership.status != UserWorkspaceStatus.INVITED:
                    existing_membership.status = UserWorkspaceStatus.INVITED
                    existing_membership.role = role
                    await db.user_workspaces.update(existing_membership)

        return token

    async def accept_invitation(
        self, invitation_id: str, user_id: str
    ) -> tuple[str, WorkspaceRole]:
        """Accept an invitation by ID and return workspace_id and role"""
        async with get_db_context() as db:
            invitation = await db.invitations.get_by_id(invitation_id)
            if not invitation:
                raise ValueError("Invitation not found")

            if invitation.accepted_at:
                raise ValueError("Invitation has already been accepted")

            if invitation.expires_at < datetime.now(timezone.utc):
                raise ValueError("Invitation has expired")

            user = await db.users.get_by_id(user_id)
            if not user:
                raise ValueError("User not found")

            if user.email.lower().strip() != invitation.email.lower().strip():
                raise ValueError("Invitation email does not match user email")

            await db.invitations.accept_invitation(invitation.id)

            existing_membership = await db.user_workspaces.get_by_user_and_workspace(
                user_id, invitation.workspace_id
            )

            if existing_membership:
                existing_membership.status = UserWorkspaceStatus.ACTIVE
                existing_membership.role = invitation.role
                await db.user_workspaces.update(existing_membership)
            else:
                membership = UserWorkspace(
                    user_id=user_id,
                    workspace_id=invitation.workspace_id,
                    role=invitation.role,
                    status=UserWorkspaceStatus.ACTIVE,
                )
                await db.user_workspaces.create(membership)

        return invitation.workspace_id, invitation.role
