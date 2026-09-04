from __future__ import annotations

import hashlib
import html
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol

from database.repositories.email_outbox import EmailOutboxRepository
from database.repositories.identity import (
    UserRepository,
    WorkspaceAuditRepository,
    WorkspaceInvitationRepository,
    WorkspaceMemberRepository,
    WorkspaceRepository,
)
from shared.app_identity import DISPLAY_NAME
from shared.email import EmailDeliveryState, EmailMessage
from shared.errors import ConflictError, InvalidInputError, NotFoundError
from shared.http.workspaces import WorkspaceAuditAction, WorkspaceAuditTarget
from shared.identity import (
    AuthTokenRecord,
    UserRecord,
    WorkspaceInvitationRecord,
    WorkspaceInvitationRole,
    WorkspaceMemberRecord,
    WorkspaceRecord,
    normalize_invitation_email,
    workspace_role_covers,
)
from shared.timestamps import utc_now
from sqlalchemy.orm import Session

from identity.auth import IdentityContext
from identity.users import display_name
from notifications import discard_queued_email, enqueue_email

INVITATION_TTL = timedelta(days=14)


class WorkspaceInvitationAdmission(Protocol):
    """Whether an offer could be honoured if it were accepted now."""

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
    """An open offer as its workspace's administrators see it.

    `expired` is decided here against one clock rather than left to whoever
    renders it, so two people looking at the same workspace cannot disagree
    about which offers are still live.
    """

    invitation: WorkspaceInvitationRecord
    invited_by_name: str = ""
    expired: bool = False
    delivery: EmailDeliveryState = EmailDeliveryState.Queued
    """What became of the message carrying this offer's link.

    Read from the outbox rather than assumed, because a message the platform
    sent successfully and a message somebody received are different facts, and
    an administrator wondering why nobody answered needs the second one.
    """


@dataclass(frozen=True, slots=True)
class InvitationPreview:
    """What a link shows the person holding it, before they answer."""

    invitation: WorkspaceInvitationRecord
    workspace: WorkspaceRecord
    invited_by_name: str = ""
    expired: bool = False


@dataclass(frozen=True, slots=True)
class AcceptedInvitation:
    """The membership an acceptance produced, and who now holds it."""

    membership: WorkspaceMemberRecord
    user: UserRecord
    workspace: WorkspaceRecord


class WorkspaceInvitationService:
    """Offers of membership, from the administrator who sends one to whoever redeems it.

    An invitation is a link. Whoever opens it, signed in as any account, joins;
    the membership binds to that account and the offer is gone. The address is
    where the message was sent and decides nothing about who may accept, because
    an address can be changed on the far side and reassigned to somebody else,
    and an offer keyed on one would follow the address rather than the person it
    was written for.

    What protects the offer is the secret in the link: 32 bytes, stored only as
    its SHA-256, redeemed under a row lock, and replaced whenever the offer is
    sent again. Nothing sends the message inline. The row and the queued message
    commit together and a drain delivers it, so no request waits on an email
    provider and no offer exists that nobody was told about.
    """

    def __init__(
        self,
        context: IdentityContext,
        *,
        invitations_url: str,
        ttl: timedelta = INVITATION_TTL,
    ) -> None:
        self.context = context
        self.invitations_url = invitations_url.rstrip("/")
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
        token = _new_token()
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
            inviter = _name_of(session, actor.user_id)
            expires_at = now + self.ttl
            # Queued first so the offer can name the message that carries it,
            # which is how the dashboard shows whether it was delivered.
            message_id = enqueue_email(
                session,
                invitation_email(
                    email=address,
                    role=role,
                    expires_at=expires_at,
                    workspace_name=workspace.name,
                    inviter=inviter,
                    link=self._link(token),
                ),
                now=now,
            )
            invitation = WorkspaceInvitationRepository(session).create(
                workspace_id=workspace.id,
                email=address,
                role=role,
                invited_by_user_id=actor.user_id,
                token_hash=_hash_token(token),
                expires_at=expires_at,
                message_id=message_id,
            )
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
        return InvitationListing(
            invitation=invitation,
            invited_by_name=inviter,
            expired=invitation.expired_at(now),
        )

    def resend(
        self,
        workspace_id: str,
        invitation_id: str,
        *,
        actor: AuthTokenRecord,
    ) -> InvitationListing:
        """Send the offer again on a new link, which is also how an expired one returns.

        The old link stops working. A resend happens because the first message
        went astray, and leaving its link live would leave whatever it went
        astray into holding a way in. The message still names whoever made the
        offer rather than whoever pressed resend, because the invitee is being
        told who wants them in and that has not changed.
        """
        now = utc_now()
        token = _new_token()
        with self.context.database.session() as session:
            workspace = _writable_workspace(session, workspace_id)
            invitations = WorkspaceInvitationRepository(session)
            invitation = _in_workspace(invitations.lock(invitation_id), workspace.id)
            inviter = _name_of(session, invitation.invited_by_user_id)
            expires_at = now + self.ttl
            message_id = enqueue_email(
                session,
                invitation_email(
                    email=invitation.email,
                    role=invitation.role,
                    expires_at=expires_at,
                    workspace_name=workspace.name,
                    inviter=inviter,
                    link=self._link(token),
                ),
                now=now,
            )
            # The earlier message carries a link this resend has just killed.
            # If it has not gone yet, it must not go: two invitations arriving
            # and the older one answering 404 reads as the platform being broken.
            discard_queued_email(session, invitation.message_id, now=now)
            invitation = invitations.reissue(
                invitation.id,
                token_hash=_hash_token(token),
                expires_at=expires_at,
                message_id=message_id,
            )
            WorkspaceAuditRepository(session).append(
                workspace_id=workspace.id,
                action=WorkspaceAuditAction.InvitationResent,
                actor=actor,
                target_type=WorkspaceAuditTarget.Invitation,
                target_id=invitation.id,
                target_name=invitation.email,
                summary=f"Resent the invitation to {invitation.email}",
            )
        return InvitationListing(
            invitation=invitation,
            invited_by_name=inviter,
            expired=invitation.expired_at(now),
        )

    def revoke(
        self,
        workspace_id: str,
        invitation_id: str,
        *,
        actor: AuthTokenRecord,
    ) -> None:
        """Withdraw the offer, which removes it. The audit history is what remembers."""
        with self.context.database.session() as session:
            workspace = _writable_workspace(session, workspace_id)
            invitations = WorkspaceInvitationRepository(session)
            invitation = _in_workspace(invitations.lock(invitation_id), workspace.id)
            invitations.delete(invitation.id)
            WorkspaceAuditRepository(session).append(
                workspace_id=workspace.id,
                action=WorkspaceAuditAction.InvitationRevoked,
                actor=actor,
                target_type=WorkspaceAuditTarget.Invitation,
                target_id=invitation.id,
                target_name=invitation.email,
                summary=f"Revoked the invitation to {invitation.email}",
            )

    def open_offers(self, workspace_id: str) -> list[InvitationListing]:
        now = utc_now()
        with self.context.database.session() as session:
            records = WorkspaceInvitationRepository(session).for_workspace(workspace_id)
            names = _names(session, [record.invited_by_user_id for record in records])
            delivery = EmailOutboxRepository(session).delivery_states(
                [record.message_id for record in records if record.message_id]
            )
            return [
                InvitationListing(
                    invitation=record,
                    invited_by_name=names.get(record.invited_by_user_id, ""),
                    expired=record.expired_at(now),
                    delivery=delivery.get(record.message_id, EmailDeliveryState.Queued),
                )
                for record in records
            ]

    def preview(self, token: str) -> InvitationPreview:
        """What the link opens onto: which workspace, who asked, and whether it still stands."""
        now = utc_now()
        with self.context.database.session() as session:
            invitation = _found(WorkspaceInvitationRepository(session).by_token(_hash_token(token)))
            workspace = WorkspaceRepository(session).get(invitation.workspace_id)
            if workspace is None:
                raise NotFoundError("invitation not found")
            return InvitationPreview(
                invitation=invitation,
                workspace=workspace,
                invited_by_name=_name_of(session, invitation.invited_by_user_id),
                expired=invitation.expired_at(now),
            )

    def accept(
        self,
        token: str,
        *,
        actor: AuthTokenRecord,
        admission: WorkspaceInvitationAdmission,
    ) -> AcceptedInvitation:
        """Redeem the link as whoever is signed in.

        Any account may redeem it, because holding the link is the claim being
        made and the address it was mailed to may not be one this person still
        reports. Redeeming under a row lock, and deleting the row, is what makes
        the offer single-use: a second click finds nothing.
        """
        now = utc_now()
        with self.context.database.session() as session:
            user = _user(session, actor.user_id)
            invitations = WorkspaceInvitationRepository(session)
            invitation = _redeemable(invitations.lock_by_token(_hash_token(token)), now=now)
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
                # Added directly while the offer stood. The offer is still an
                # administrator's live decision, so it grants what it named.
                members.set_role(workspace_id=workspace.id, user_id=user.id, role=offered)
                membership = members.membership(workspace_id=workspace.id, user_id=user.id)
                if membership is None:
                    raise NotFoundError(f"user is not a member of this workspace: {user.id}")
            invitations.delete(invitation.id)
            WorkspaceAuditRepository(session).append(
                workspace_id=workspace.id,
                action=WorkspaceAuditAction.InvitationAccepted,
                actor=actor,
                target_type=WorkspaceAuditTarget.Member,
                target_id=user.id,
                target_name=display_name(user),
                summary=f"Accepted an invitation sent to {invitation.email}, "
                f"joining as {membership.role}",
                new_value=membership.role.value,
            )
            return AcceptedInvitation(membership=membership, user=user, workspace=workspace)

    def decline(self, token: str, *, actor: AuthTokenRecord) -> None:
        now = utc_now()
        with self.context.database.session() as session:
            invitations = WorkspaceInvitationRepository(session)
            invitation = _redeemable(invitations.lock_by_token(_hash_token(token)), now=now)
            invitations.delete(invitation.id)
            WorkspaceAuditRepository(session).append(
                workspace_id=invitation.workspace_id,
                action=WorkspaceAuditAction.InvitationDeclined,
                actor=actor,
                target_type=WorkspaceAuditTarget.Invitation,
                target_id=invitation.id,
                target_name=invitation.email,
                summary=f"Declined an invitation sent to {invitation.email}",
            )

    def _link(self, token: str) -> str:
        return f"{self.invitations_url}/{token}"


def invitation_email(
    *,
    email: str,
    role: WorkspaceInvitationRole,
    expires_at: datetime,
    workspace_name: str,
    inviter: str,
    link: str,
) -> EmailMessage:
    """What the invited person reads: who asked, into what, and the link that joins.

    The link is the whole of the offer, so the message says plainly that it is
    for them alone. Whoever opens it joins as whichever account they are signed
    in as, which is what makes forwarding it a way to hand somebody else a seat.
    """
    who = inviter or f"A {DISPLAY_NAME} administrator"
    described_role = (
        "an administrator" if role is WorkspaceInvitationRole.Administrator else "a member"
    )
    expires = expires_at.strftime("%B %d, %Y")
    subject = f"{who} invited you to {workspace_name} on {DISPLAY_NAME}"
    text = (
        f"{who} invited you to join the workspace {workspace_name} on {DISPLAY_NAME} "
        f"as {described_role}.\n\n"
        f"Open this link to accept. You will be asked to sign in first if you are not "
        f"already:\n{link}\n\n"
        f"The link joins as whichever account you are signed in as, so keep it to "
        f"yourself. It expires on {expires}. If you were not expecting this, you can "
        "ignore this message."
    )
    h = html.escape
    body = (
        '<div style="font-family:-apple-system,BlinkMacSystemFont,Segoe UI,Roboto,'
        "Helvetica,Arial,sans-serif;max-width:560px;margin:0 auto;padding:32px 24px;"
        'color:#1a1a1a;line-height:1.5">'
        '<h1 style="font-size:20px;margin:0 0 16px">'
        f"You have been invited to {h(workspace_name)}</h1>"
        f"<p><strong>{h(who)}</strong> invited you to join the workspace "
        f"<strong>{h(workspace_name)}</strong> on {h(DISPLAY_NAME)} as {described_role}.</p>"
        f'<p style="margin:24px 0"><a href="{h(link)}" style="display:inline-block;'
        "background:#1a1a1a;color:#ffffff;padding:12px 20px;border-radius:6px;"
        'text-decoration:none;font-weight:600">Accept invitation</a></p>'
        '<p style="color:#555;font-size:14px">You will be asked to sign in first if you '
        "are not already. The link joins as whichever account you are signed in as, so "
        f"keep it to yourself. It expires on {h(expires)}.</p>"
        '<p style="color:#888;font-size:13px">If you were not expecting this, you can ignore '
        "this message.</p></div>"
    )
    return EmailMessage(to=email, subject=subject, html=body, text=text)


def _new_token() -> str:
    return secrets.token_urlsafe(32)


def _hash_token(token: str) -> str:
    """What the database holds. A dump is then a list of offers, not a set of keys."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _writable_workspace(session: Session, workspace_id: str) -> WorkspaceRecord:
    """The workspace, fenced against a deletion running beside this write."""
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


def _found(invitation: WorkspaceInvitationRecord | None) -> WorkspaceInvitationRecord:
    if invitation is None:
        raise NotFoundError("invitation not found")
    return invitation


def _in_workspace(
    invitation: WorkspaceInvitationRecord | None,
    workspace_id: str,
) -> WorkspaceInvitationRecord:
    """The offer, if it is this workspace's.

    One that does not exist and one belonging to another workspace answer the
    same way, so an administrator here learns nothing about offers elsewhere.
    """
    if invitation is None or invitation.workspace_id != workspace_id:
        raise NotFoundError("invitation not found")
    return invitation


def _redeemable(
    invitation: WorkspaceInvitationRecord | None,
    *,
    now: datetime,
) -> WorkspaceInvitationRecord:
    """The offer, if the link still opens it.

    An unknown token and a spent one answer the same way, because a redeemed
    offer leaves no row and there is nothing to tell them apart with.
    """
    invitation = _found(invitation)
    if invitation.expired_at(now):
        raise ConflictError("this invitation has expired; ask for a new one")
    return invitation


__all__ = [
    "INVITATION_TTL",
    "AcceptedInvitation",
    "InvitationListing",
    "InvitationPreview",
    "WorkspaceInvitationAdmission",
    "WorkspaceInvitationService",
    "invitation_email",
]
