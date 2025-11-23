import secrets
from datetime import datetime, timedelta, timezone

from lazycloud_api.config import app_config
from lazycloud_api.database import db
from lazycloud_api.database.invitations import WorkspaceInvitationService
from lazycloud_api.database.user_workspaces import (
    UserWorkspaceService,
    UserWorkspaceStatus,
    WorkspaceRole,
)


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
        invitation_type: str = "member",
    ) -> str:
        """Create a new invitation or resend an existing one, return the token"""
        email = email.lower().strip()

        existing_user = await db.users.get_by_email(email)
        existing_invitation = await self.invitation_service.get_by_workspace_and_email(
            workspace_id, email
        )

        if existing_invitation:
            if existing_invitation.accepted_at:
                raise ValueError("User has already accepted an invitation")

            existing_invitation.expires_at = self._calculate_expires_at()
            existing_invitation.role = role
            existing_invitation.invitation_type = invitation_type
            await self.invitation_service.update(existing_invitation)
            return existing_invitation.token

        token = self._generate_token()
        expires_at = self._calculate_expires_at()

        await self.invitation_service.create_invitation(
            workspace_id=workspace_id,
            email=email,
            role=role,
            invited_by_user_id=invited_by_user_id,
            token=token,
            expires_at=expires_at,
            invitation_type=invitation_type,
        )

        if existing_user:
            existing_membership = (
                await self.user_workspace_service.get_by_user_and_workspace(
                    existing_user.id, workspace_id
                )
            )

            if not existing_membership:
                await self.user_workspace_service.create(
                    {
                        "user_id": existing_user.id,
                        "workspace_id": workspace_id,
                        "role": role,
                        "status": UserWorkspaceStatus.INVITED,
                    }
                )

        return token

    async def accept_invitation(
        self, token: str, user_id: str
    ) -> tuple[str, WorkspaceRole]:
        """Accept an invitation and return workspace_id and role"""
        invitation = await self.invitation_service.get_by_token(token)
        if not invitation:
            raise ValueError("Invalid invitation token")

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
                    user_id, invitation.workspace_id
                )
            )

            if existing_membership:
                existing_membership.status = UserWorkspaceStatus.ACTIVE
                existing_membership.role = invitation.role
                await self.user_workspace_service.update(
                    existing_membership, session=session
                )

            else:
                await self.user_workspace_service.create(
                    {
                        "user_id": user_id,
                        "workspace_id": invitation.workspace_id,
                        "role": invitation.role,
                        "status": UserWorkspaceStatus.ACTIVE,
                    },
                    session=session,
                )

        return invitation.workspace_id, invitation.role
