from __future__ import annotations

import html
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta

from database.repositories.identity import (
    UserRepository,
    WorkspaceAuditRepository,
    WorkspaceInvitationRepository,
    WorkspaceMemberRepository,
    WorkspaceRepository,
)
from shared.app_identity import DISPLAY_NAME
from shared.email import EmailMessage, EmailSender
from shared.errors import ConflictError, InvalidInputError, NotFoundError, UpstreamUnavailableError
from shared.http.workspaces import WorkspaceAuditAction, WorkspaceAuditTarget
from shared.identity import (
    AuthTokenRecord,
    UserRecord,
    WorkspaceInvitationRecord,
    WorkspaceInvitationStatus,
    WorkspaceMemberRecord,
    WorkspaceRecord,
    WorkspaceRole,
    WorkspaceStatus,
    normalize_invitation_email,
)
from shared.timestamps import utc_now
from sqlalchemy.orm import Session

from identity.auth import IdentityContext
from identity.users import WorkspaceMembershipAdmission

INVITATION_TTL = timedelta(days=14)


@dataclass(frozen=True, slots=True)
class InvitationListing:
    """An invitation beside the name of whoever sent it."""

    invitation: WorkspaceInvitationRecord
    invited_by_name: str = ""


@dataclass(frozen=True, slots=True)
class PendingInvitation:
    """An invitation as the person it was sent to sees it."""

    invitation: WorkspaceInvitationRecord
    workspace: WorkspaceRecord
    invited_by_name: str = ""


class WorkspaceInvitationService:
    """Offers of membership, from the administrator who sends one to the person who answers it.

    An invitation is addressed to an email, and the only account that can answer
    it is one whose provider-verified address is that email at the moment it
    answers. That comparison is the single place an email decides anything about
    identity here, and it happens under a row lock so two answers to one
    invitation cannot both succeed.
    """

    def __init__(
        self,
        context: IdentityContext,
        *,
        mailer: Callable[[], EmailSender],
        invitations_url: str,
        ttl: timedelta = INVITATION_TTL,
    ) -> None:
        self.context = context
        self.mailer = mailer
        self.invitations_url = invitations_url
        self.ttl = ttl

    def invite(
        self,
        workspace_id: str,
        *,
        email: str,
        role: WorkspaceRole,
        actor: AuthTokenRecord,
        admission: WorkspaceMembershipAdmission,
    ) -> InvitationListing:
        if role is WorkspaceRole.Owner:
            raise InvalidInputError(
                "a workspace has exactly one owner; invite an administrator or member"
            )
        try:
            email = normalize_invitation_email(email)
        except ValueError as exc:
            raise InvalidInputError(str(exc)) from exc
        now = utc_now()
        with self.context.database.session() as session:
            workspace = _active_workspace(session, workspace_id)
            members = WorkspaceMemberRepository(session)
            if members.member_with_email(workspace_id=workspace.id, email=email) is not None:
                raise ConflictError(f"{email} is already a member of this workspace")
            invitations = WorkspaceInvitationRepository(session)
            if invitations.pending_for_email(workspace_id=workspace.id, email=email) is not None:
                raise ConflictError(f"an invitation for {email} is already pending; resend it")
            admission.assert_may_add_workspace_member(
                session,
                workspace_id=workspace.id,
                member_user_id=None,
            )
            invitation = invitations.create(
                workspace_id=workspace.id,
                email=email,
                role=role,
                invited_by_user_id=actor.user_id,
                expires_at=now + self.ttl,
            )
            inviter = _display_name(session, actor)
            WorkspaceAuditRepository(session).append(
                workspace_id=workspace.id,
                action=WorkspaceAuditAction.MemberInvited,
                actor=actor,
                target_type=WorkspaceAuditTarget.Invitation,
                target_id=invitation.id,
                target_name=email,
                summary=f"Invited {email} as {role}",
                new_value=role.value,
            )
        self._deliver(invitation, workspace, inviter)
        return InvitationListing(invitation=invitation, invited_by_name=inviter)

    def resend(
        self,
        workspace_id: str,
        invitation_id: str,
        *,
        actor: AuthTokenRecord,
    ) -> InvitationListing:
        """Send the same offer again, and give it a fresh expiry while at it."""
        now = utc_now()
        with self.context.database.session() as session:
            workspace = _active_workspace(session, workspace_id)
            invitations = WorkspaceInvitationRepository(session)
            invitation = _pending_in_workspace(invitations.lock(invitation_id), workspace.id)
            invitation = invitations.extend(invitation.id, expires_at=now + self.ttl)
            inviter = _display_name(session, actor)
            WorkspaceAuditRepository(session).append(
                workspace_id=workspace.id,
                action=WorkspaceAuditAction.InvitationResent,
                actor=actor,
                target_type=WorkspaceAuditTarget.Invitation,
                target_id=invitation.id,
                target_name=invitation.email,
                summary=f"Resent the invitation to {invitation.email}",
            )
        self._deliver(invitation, workspace, inviter)
        return InvitationListing(invitation=invitation, invited_by_name=inviter)

    def revoke(
        self,
        workspace_id: str,
        invitation_id: str,
        *,
        actor: AuthTokenRecord,
    ) -> WorkspaceInvitationRecord:
        now = utc_now()
        with self.context.database.session() as session:
            workspace = _active_workspace(session, workspace_id)
            invitations = WorkspaceInvitationRepository(session)
            invitation = _pending_in_workspace(invitations.lock(invitation_id), workspace.id)
            revoked = invitations.resolve(
                invitation.id,
                status=WorkspaceInvitationStatus.Revoked,
                resolved_by_user_id=actor.user_id,
                now=now,
            )
            WorkspaceAuditRepository(session).append(
                workspace_id=workspace.id,
                action=WorkspaceAuditAction.InvitationRevoked,
                actor=actor,
                target_type=WorkspaceAuditTarget.Invitation,
                target_id=invitation.id,
                target_name=invitation.email,
                summary=f"Revoked the invitation to {invitation.email}",
            )
            return revoked

    def pending(self, workspace_id: str) -> list[InvitationListing]:
        with self.context.database.session() as session:
            records = WorkspaceInvitationRepository(session).pending_for_workspace(workspace_id)
            names = _names(session, [record.invited_by_user_id for record in records])
            return [
                InvitationListing(
                    invitation=record,
                    invited_by_name=names.get(record.invited_by_user_id, ""),
                )
                for record in records
            ]

    def pending_for_user(self, user_id: str) -> list[PendingInvitation]:
        """Everything the signed-in person can still answer, by their verified email."""
        with self.context.database.session() as session:
            user = _user(session, user_id)
            email = _matching_email(user)
            if not email:
                return []
            rows = WorkspaceInvitationRepository(session).open_for_email(email, now=utc_now())
            names = _names(session, [invitation.invited_by_user_id for invitation, _ in rows])
            return [
                PendingInvitation(
                    invitation=invitation,
                    workspace=workspace,
                    invited_by_name=names.get(invitation.invited_by_user_id, ""),
                )
                for invitation, workspace in rows
            ]

    def accept(
        self,
        invitation_id: str,
        *,
        actor: AuthTokenRecord,
        admission: WorkspaceMembershipAdmission,
    ) -> WorkspaceMemberRecord:
        now = utc_now()
        with self.context.database.session() as session:
            user = _user(session, actor.user_id)
            invitations = WorkspaceInvitationRepository(session)
            invitation = _answerable(invitations.lock(invitation_id), user, now=now)
            workspace = _active_workspace(session, invitation.workspace_id)
            members = WorkspaceMemberRepository(session)
            membership = members.membership(workspace_id=workspace.id, user_id=user.id)
            if membership is None:
                admission.assert_may_add_workspace_member(
                    session,
                    workspace_id=workspace.id,
                    member_user_id=user.id,
                )
                membership = members.add(
                    workspace_id=workspace.id,
                    user_id=user.id,
                    role=invitation.role,
                )
            invitations.resolve(
                invitation.id,
                status=WorkspaceInvitationStatus.Accepted,
                resolved_by_user_id=user.id,
                now=now,
            )
            WorkspaceAuditRepository(session).append(
                workspace_id=workspace.id,
                action=WorkspaceAuditAction.InvitationAccepted,
                actor=actor,
                target_type=WorkspaceAuditTarget.Member,
                target_id=user.id,
                target_name=user.display_name or user.email,
                summary=f"Accepted an invitation and joined as {membership.role}",
                new_value=membership.role.value,
            )
            return membership

    def decline(self, invitation_id: str, *, actor: AuthTokenRecord) -> WorkspaceInvitationRecord:
        now = utc_now()
        with self.context.database.session() as session:
            user = _user(session, actor.user_id)
            invitations = WorkspaceInvitationRepository(session)
            invitation = _answerable(invitations.lock(invitation_id), user, now=now)
            declined = invitations.resolve(
                invitation.id,
                status=WorkspaceInvitationStatus.Declined,
                resolved_by_user_id=user.id,
                now=now,
            )
            WorkspaceAuditRepository(session).append(
                workspace_id=invitation.workspace_id,
                action=WorkspaceAuditAction.InvitationDeclined,
                actor=actor,
                target_type=WorkspaceAuditTarget.Invitation,
                target_id=invitation.id,
                target_name=invitation.email,
                summary="Declined an invitation",
            )
            return declined

    def _deliver(
        self,
        invitation: WorkspaceInvitationRecord,
        workspace: WorkspaceRecord,
        inviter: str,
    ) -> None:
        """Send after the row is committed, so a refusal leaves an invitation to resend.

        The row is the durable fact and the email is its delivery. Reporting the
        refusal, with the invitation standing, is what lets an administrator retry
        once the provider is back rather than re-invite and race a second row.
        """
        message = invitation_email(
            invitation,
            workspace_name=workspace.name,
            inviter=inviter,
            invitations_url=self.invitations_url,
        )
        try:
            self.mailer().send(message)
        except UpstreamUnavailableError as exc:
            raise UpstreamUnavailableError(
                f"the invitation to {invitation.email} was recorded but the email was not "
                f"delivered; resend it once email delivery is restored. {exc}"
            ) from exc


def invitation_email(
    invitation: WorkspaceInvitationRecord,
    *,
    workspace_name: str,
    inviter: str,
    invitations_url: str,
) -> EmailMessage:
    """What the invited person reads: who asked, into what, and where to answer.

    Nothing in the link is a credential. The page it opens asks the person to sign
    in and then shows what is addressed to the email they signed in with, so a
    forwarded message reaches nobody it was not sent to.
    """
    who = inviter or f"A {DISPLAY_NAME} administrator"
    role = "an administrator" if invitation.role is WorkspaceRole.Administrator else "a member"
    expires = invitation.expires_at.strftime("%B %d, %Y")
    subject = f"{who} invited you to {workspace_name} on {DISPLAY_NAME}"
    text = (
        f"{who} invited you to join the workspace {workspace_name} on {DISPLAY_NAME} "
        f"as {role}.\n\n"
        f"Sign in with the GitHub account whose primary email is {invitation.email} "
        f"to accept or decline:\n{invitations_url}\n\n"
        f"This invitation expires on {expires}. If you were not expecting it, "
        "you can ignore this message."
    )
    h = html.escape
    body = (
        '<div style="font-family:-apple-system,BlinkMacSystemFont,Segoe UI,Roboto,'
        "Helvetica,Arial,sans-serif;max-width:560px;margin:0 auto;padding:32px 24px;"
        'color:#1a1a1a;line-height:1.5">'
        '<h1 style="font-size:20px;margin:0 0 16px">'
        f"You have been invited to {h(workspace_name)}</h1>"
        f"<p><strong>{h(who)}</strong> invited you to join the workspace "
        f"<strong>{h(workspace_name)}</strong> on {h(DISPLAY_NAME)} as {role}.</p>"
        f'<p style="margin:24px 0"><a href="{h(invitations_url)}" style="display:inline-block;'
        "background:#1a1a1a;color:#ffffff;padding:12px 20px;border-radius:6px;"
        'text-decoration:none;font-weight:600">View invitation</a></p>'
        f'<p style="color:#555;font-size:14px">Sign in with the GitHub account whose primary '
        f"email is <strong>{h(invitation.email)}</strong> to accept or decline. "
        f"This invitation expires on {h(expires)}.</p>"
        '<p style="color:#888;font-size:13px">If you were not expecting this, you can ignore '
        "this message.</p></div>"
    )
    return EmailMessage(to=invitation.email, subject=subject, html=body, text=text)


def _active_workspace(session: Session, workspace_id: str) -> WorkspaceRecord:
    workspace = WorkspaceRepository(session).get(workspace_id)
    if workspace is None:
        raise NotFoundError(f"workspace not found: {workspace_id}")
    if workspace.status is not WorkspaceStatus.Active:
        raise ConflictError(f"workspace is not active: {workspace.name}")
    return workspace


def _user(session: Session, user_id: str) -> UserRecord:
    user = UserRepository(session).get(user_id) if user_id else None
    if user is None:
        raise NotFoundError(f"user not found: {user_id}")
    return user


def _names(session: Session, user_ids: list[str]) -> dict[str, str]:
    users = UserRepository(session).for_ids([user_id for user_id in user_ids if user_id])
    return {user_id: user.display_name or user.email for user_id, user in users.items()}


def _display_name(session: Session, actor: AuthTokenRecord) -> str:
    if not actor.user_id:
        return ""
    return _names(session, [actor.user_id]).get(actor.user_id, "")


def _matching_email(user: UserRecord) -> str:
    """The address an invitation is compared against: the provider's, folded the same way."""
    return user.email.strip().lower()


def _pending_in_workspace(
    invitation: WorkspaceInvitationRecord | None,
    workspace_id: str,
) -> WorkspaceInvitationRecord:
    """The invitation, if it is this workspace's and still open to be acted on.

    One not found and one belonging to another workspace answer the same way: an
    administrator of this workspace learns nothing about invitations elsewhere.
    """
    if invitation is None or invitation.workspace_id != workspace_id:
        raise NotFoundError("invitation not found")
    if invitation.status is not WorkspaceInvitationStatus.Pending:
        raise ConflictError(f"the invitation was already {invitation.status}")
    return invitation


def _answerable(
    invitation: WorkspaceInvitationRecord | None,
    user: UserRecord,
    *,
    now: datetime,
) -> WorkspaceInvitationRecord:
    """The invitation, if this person is who it was sent to and it is still open.

    A mismatched address is reported as not found rather than forbidden, so an
    invitation id says nothing about which address it is waiting on.
    """
    email = _matching_email(user)
    if invitation is None or not email or invitation.email != email:
        raise NotFoundError("invitation not found")
    if invitation.status is not WorkspaceInvitationStatus.Pending:
        raise ConflictError(f"the invitation was already {invitation.status}")
    if invitation.expires_at <= now:
        raise ConflictError("the invitation has expired; ask to be invited again")
    return invitation


__all__ = [
    "INVITATION_TTL",
    "InvitationListing",
    "PendingInvitation",
    "WorkspaceInvitationService",
    "invitation_email",
]
