import secrets
from datetime import datetime, timedelta, timezone

from lazycloud_api.config import app_config
from lazycloud_api.database import db
from lazycloud_api.database.invitations import WorkspaceInvitationService
from lazycloud_api.database.user_workspaces import (
    UserWorkspacePydantic,
    UserWorkspaceService,
    UserWorkspaceStatus,
    WorkspaceRole,
)
from shared.models.workspaces import InvitationType


class InvitationService:
    """Service for managing workspace invitations"""

    def __init__(self):
        self.invitation_service: WorkspaceInvitationService = (
            WorkspaceInvitationService()
        )
        self.user_workspace_service: UserWorkspaceService = UserWorkspaceService()

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
        """Create a new invitation or resend an existing one, return the token"""
        email = email.lower().strip()

        existing_user = await db.users.get_by_email(email)

        # For ownership transfers, only check for existing ownership transfer invitations
        # For member invitations, check for any existing pending invitation
        if invitation_type == InvitationType.OWNERSHIP_TRANSFER:
            # Check for any pending ownership transfer invitation for this workspace
            # (not just for this email, but for any email - only one ownership transfer at a time)
            workspace_invitations = await self.invitation_service.get_by_workspace(
                workspace_id, include_accepted=False
            )
            existing_ownership_transfer = next(
                (
                    inv
                    for inv in workspace_invitations
                    if inv.invitation_type == InvitationType.OWNERSHIP_TRANSFER.value
                    and not inv.accepted_at
                ),
                None,
            )

            if existing_ownership_transfer:
                raise ValueError(
                    "An ownership transfer invitation is already pending. "
                    "Please cancel the existing invitation before sending a new one."
                )

            # Also check for existing invitation for this specific email
            existing_invitation = (
                await self.invitation_service.get_by_workspace_and_email_and_type(
                    workspace_id, email, invitation_type.value
                )
            )
        else:
            # For regular member invitations, check if user is already an active member
            is_currently_member = False
            if existing_user:
                current_membership = (
                    await self.user_workspace_service.get_by_user_and_workspace(
                        existing_user.id, workspace_id
                    )
                )
                is_currently_member = (
                    current_membership is not None
                    and current_membership.status == UserWorkspaceStatus.ACTIVE
                )

            # Prevent inviting users who are already active members (only for member invitations)
            if is_currently_member:
                raise ValueError("User is already an active member of this workspace")

            existing_invitation = (
                await self.invitation_service.get_by_workspace_and_email(
                    workspace_id, email
                )
            )

        # Wrap all operations in a transaction to ensure atomicity
        async with self.invitation_service.transaction() as session:
            # If there's an existing pending invitation (not accepted), cancel it and create a new one
            # This ensures each resend creates a fresh invitation with a new token
            if existing_invitation and not existing_invitation.accepted_at:
                # Cancel the old invitation by deleting it using repository method
                await self.invitation_service.delete(
                    existing_invitation.id, session=session
                )

                # Also clean up any INVITED status user_workspace record
                if existing_user:
                    existing_membership = (
                        await self.user_workspace_service.get_by_user_and_workspace(
                            existing_user.id, workspace_id, session=session
                        )
                    )
                    if (
                        existing_membership
                        and existing_membership.status == UserWorkspaceStatus.INVITED
                    ):
                        await self.user_workspace_service.delete(
                            existing_membership.id, session=session
                        )

            # If there's an accepted invitation but user is no longer a member, allow creating a new one
            # (The old accepted invitation stays for history, but we create a new pending one)

            # Create a new invitation with a fresh token
            token = self._generate_token()
            expires_at = self._calculate_expires_at()

            await self.invitation_service.create_invitation(
                workspace_id=workspace_id,
                email=email,
                role=role,
                invited_by_user_id=invited_by_user_id,
                token=token,
                expires_at=expires_at,
                invitation_type=invitation_type.value,
                session=session,
            )

            # Create or update user_workspace record with INVITED status
            # Skip this for ownership transfers since the user is already an active member
            if invitation_type != InvitationType.OWNERSHIP_TRANSFER and existing_user:
                existing_membership = (
                    await self.user_workspace_service.get_by_user_and_workspace(
                        existing_user.id, workspace_id, session=session
                    )
                )

                if not existing_membership:
                    membership = UserWorkspacePydantic(
                        user_id=existing_user.id,
                        workspace_id=workspace_id,
                        role=role,
                        status=UserWorkspaceStatus.INVITED,
                    )
                    await self.user_workspace_service.create(
                        membership, session=session
                    )
                elif existing_membership.status != UserWorkspaceStatus.INVITED:
                    # If membership exists but not in INVITED status, update it
                    existing_membership.status = UserWorkspaceStatus.INVITED
                    existing_membership.role = role
                    await self.user_workspace_service.update(
                        existing_membership, session=session
                    )

        return token

    async def accept_invitation(
        self, invitation_id: str, user_id: str
    ) -> tuple[str, WorkspaceRole]:
        """Accept an invitation by ID and return workspace_id and role"""
        invitation = await self.invitation_service.get_by_id(invitation_id)
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

        async with self.invitation_service.transaction() as session:
            await self.invitation_service.accept_invitation(
                invitation.id, session=session
            )

            existing_membership = (
                await self.user_workspace_service.get_by_user_and_workspace(
                    user_id, invitation.workspace_id, session=session
                )
            )

            if existing_membership:
                existing_membership.status = UserWorkspaceStatus.ACTIVE
                existing_membership.role = invitation.role
                await self.user_workspace_service.update(
                    existing_membership, session=session
                )

            else:
                membership = UserWorkspacePydantic(
                    user_id=user_id,
                    workspace_id=invitation.workspace_id,
                    role=invitation.role,
                    status=UserWorkspaceStatus.ACTIVE,
                )
                await self.user_workspace_service.create(membership, session=session)

        return invitation.workspace_id, invitation.role
