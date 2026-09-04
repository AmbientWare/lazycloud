from __future__ import annotations

import html
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol

from database.repositories.identity import (
    UserRepository,
    WorkspaceAuditRepository,
    WorkspaceInvitationRepository,
    WorkspaceMemberRepository,
    WorkspaceRepository,
)
from shared.app_identity import DISPLAY_NAME
from shared.deployment_settings import MissingDeploymentSettingError
from shared.email import EmailMessage, EmailSender
from shared.errors import ConflictError, InvalidInputError, NotFoundError, UpstreamUnavailableError
from shared.http.workspaces import WorkspaceAuditAction, WorkspaceAuditTarget
from shared.identity import (
    AuthTokenRecord,
    UserRecord,
    WorkspaceInvitationRecord,
    WorkspaceInvitationRole,
    WorkspaceInvitationStatus,
    WorkspaceMemberRecord,
    WorkspaceRecord,
    fold_email,
    normalize_invitation_email,
    workspace_role_covers,
)
from shared.timestamps import utc_now
from sqlalchemy.orm import Session

from identity.auth import IdentityContext
from identity.users import display_name

INVITATION_TTL = timedelta(days=14)


class WorkspaceInvitationAdmission(Protocol):
    """Whether an offer to this address could be honoured if it were accepted now."""

    def assert_may_invite_workspace_member(
        self,
        session: Session,
        *,
        workspace_id: str,
        email: str,
    ) -> None: ...

    def assert_may_add_workspace_member(
        self,
        session: Session,
        *,
        workspace_id: str,
        member_user_id: str,
    ) -> None: ...


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


@dataclass(frozen=True, slots=True)
class AcceptedInvitation:
    """The membership an acceptance produced, and who now holds it."""

    membership: WorkspaceMemberRecord
    user: UserRecord


class WorkspaceInvitationService:
    """Offers of membership, from the administrator who sends one to the person who answers it.

    An invitation is addressed to an email, and the only account that can answer
    it is one whose provider-reported address is that email. That comparison is
    the single place an email decides anything about identity here, and it
    happens under a row lock so two answers to one invitation cannot both
    succeed. The address is the one the provider last told us about at sign-in,
    so somebody who changes their primary address answers under the new one from
    their next sign-in onwards.
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
        role: WorkspaceInvitationRole,
        actor: AuthTokenRecord,
        admission: WorkspaceInvitationAdmission,
    ) -> InvitationListing:
        try:
            address = normalize_invitation_email(email)
        except ValueError as exc:
            raise InvalidInputError(str(exc)) from exc
        now = utc_now()
        with self.context.database.session() as session:
            workspace = _writable_workspace(session, workspace_id)
            members = WorkspaceMemberRepository(session)
            if members.member_with_email(workspace_id=workspace.id, email=address) is not None:
                raise ConflictError(f"{address} is already a member of this workspace")
            admission.assert_may_invite_workspace_member(
                session,
                workspace_id=workspace.id,
                email=address,
            )
            # The partial unique index is what refuses a second open offer; the
            # repository turns it into the conflict that names the resend path.
            invitation = WorkspaceInvitationRepository(session).create(
                workspace_id=workspace.id,
                email=address,
                role=role,
                invited_by_user_id=actor.user_id,
                expires_at=now + self.ttl,
            )
            inviter = _name_of(session, actor.user_id)
            WorkspaceAuditRepository(session).append(
                workspace_id=workspace.id,
                action=WorkspaceAuditAction.MemberInvited,
                actor=actor,
                target_type=WorkspaceAuditTarget.Invitation,
                target_id=invitation.id,
                target_name=address,
                summary=f"Invited {address} as {role}",
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
        """Send the same offer again, with a fresh expiry, which is how an expired one returns.

        The message still names whoever made the offer rather than whoever
        pressed resend: the invitee is being told who wants them in, and that did
        not change.
        """
        now = utc_now()
        with self.context.database.session() as session:
            workspace = _writable_workspace(session, workspace_id)
            invitations = WorkspaceInvitationRepository(session)
            invitation = _pending_in_workspace(invitations.lock(invitation_id), workspace.id)
            invitation = invitations.extend(invitation.id, expires_at=now + self.ttl)
            inviter = _name_of(session, invitation.invited_by_user_id)
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
            workspace = _writable_workspace(session, workspace_id)
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
        """Everything the signed-in person can still answer, by the address they signed in with."""
        with self.context.database.session() as session:
            user = _user(session, user_id)
            address = fold_email(user.email)
            if not address:
                return []
            rows = WorkspaceInvitationRepository(session).open_for_email(address, now=utc_now())
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
        admission: WorkspaceInvitationAdmission,
    ) -> AcceptedInvitation:
        now = utc_now()
        with self.context.database.session() as session:
            user = _user(session, actor.user_id)
            invitations = WorkspaceInvitationRepository(session)
            invitation = _answerable(invitations.lock(invitation_id), user, now=now)
            workspace = _writable_workspace(session, invitation.workspace_id)
            members = WorkspaceMemberRepository(session)
            membership = members.membership(workspace_id=workspace.id, user_id=user.id)
            offered = invitation.role.workspace_role
            if membership is None:
                admission.assert_may_add_workspace_member(
                    session,
                    workspace_id=workspace.id,
                    member_user_id=user.id,
                )
                membership = members.add(
                    workspace_id=workspace.id,
                    user_id=user.id,
                    role=offered,
                )
            elif not workspace_role_covers(membership.role, offered):
                # Somebody was added directly while their invitation stood. The
                # offer is still an administrator's live decision, so accepting
                # grants what it named rather than consuming it for nothing.
                members.set_role(workspace_id=workspace.id, user_id=user.id, role=offered)
                membership = members.membership(workspace_id=workspace.id, user_id=user.id)
                if membership is None:
                    raise NotFoundError(f"user is not a member of this workspace: {user.id}")
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
                target_name=display_name(user),
                summary=f"Accepted an invitation and joined as {membership.role}",
                new_value=membership.role.value,
            )
            return AcceptedInvitation(membership=membership, user=user)

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

        The row is the durable fact and the message is its delivery. Both failures
        that reach here, a provider that refused and a deployment with no provider
        configured, are reported with the invitation left standing, because the
        thing to do about either is resend it once the cause is fixed. A missing
        setting is translated rather than left to the generic handler: it arrives
        as a `RuntimeError` naming the variable, and that name is the only thing
        an operator can act on.
        """
        message = invitation_email(
            invitation,
            workspace_name=workspace.name,
            inviter=inviter,
            invitations_url=self.invitations_url,
        )
        try:
            self.mailer().send(message)
        except MissingDeploymentSettingError as exc:
            raise UpstreamUnavailableError(
                f"the invitation to {invitation.email} was recorded but this deployment "
                f"cannot send email. Resend it once that is configured. {exc}"
            ) from exc
        except (InvalidInputError, UpstreamUnavailableError) as exc:
            raise UpstreamUnavailableError(
                f"the invitation to {invitation.email} was recorded but the email was not "
                f"delivered. Resend it once email delivery is restored. {exc}"
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
    role = (
        "an administrator"
        if invitation.role is WorkspaceInvitationRole.Administrator
        else "a member"
    )
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
        '<p style="color:#555;font-size:14px">Sign in with the GitHub account whose primary '
        f"email is <strong>{h(invitation.email)}</strong> to accept or decline. "
        f"This invitation expires on {h(expires)}.</p>"
        '<p style="color:#888;font-size:13px">If you were not expecting this, you can ignore '
        "this message.</p></div>"
    )
    return EmailMessage(to=invitation.email, subject=subject, html=body, text=text)


def _writable_workspace(session: Session, workspace_id: str) -> WorkspaceRecord:
    """The workspace, fenced against a deletion running beside this write.

    The same key-share lock every other tenant-owned write takes: a membership or
    an invitation committed after the purge began would be state the deleted
    workspace cannot own.
    """
    return WorkspaceRepository(session).lock_active_owner(workspace_id)


def _user(session: Session, user_id: str) -> UserRecord:
    user = UserRepository(session).get(user_id) if user_id else None
    if user is None:
        raise NotFoundError(f"user not found: {user_id}")
    return user


def _names(session: Session, user_ids: list[str]) -> dict[str, str]:
    users = UserRepository(session).for_ids([user_id for user_id in user_ids if user_id])
    return {user_id: display_name(user) for user_id, user in users.items()}


def _name_of(session: Session, user_id: str) -> str:
    if not user_id:
        return ""
    return _names(session, [user_id]).get(user_id, "")


def _pending_in_workspace(
    invitation: WorkspaceInvitationRecord | None,
    workspace_id: str,
) -> WorkspaceInvitationRecord:
    """The invitation, if it is this workspace's and nobody has answered it.

    One that does not exist and one belonging to another workspace answer the
    same way, so an administrator of this workspace learns nothing about
    invitations elsewhere.
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
    invitation id says nothing about which address it is waiting on. Both sides
    of the comparison are folded by `fold_email`, because an address stored one
    way and read another is how the person it was sent to gets refused.
    """
    address = fold_email(user.email)
    if invitation is None or not address or invitation.email != address:
        raise NotFoundError("invitation not found")
    if invitation.status is not WorkspaceInvitationStatus.Pending:
        raise ConflictError(f"the invitation was already {invitation.status}")
    if invitation.expires_at <= now:
        raise ConflictError("the invitation has expired; ask to be invited again")
    return invitation


__all__ = [
    "INVITATION_TTL",
    "AcceptedInvitation",
    "InvitationListing",
    "PendingInvitation",
    "WorkspaceInvitationAdmission",
    "WorkspaceInvitationService",
    "invitation_email",
]
