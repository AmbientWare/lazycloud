from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta

import pytest
from api.server.services import ApiServices
from database.repositories.billing import BillingAccountRepository
from database.repositories.identity import UserRepository
from identity.auth import AuthService
from identity.invitations import WorkspaceInvitationService
from shared.billing_accounts import BillingAccountStatus
from shared.billing_plans import BillingPlanId
from shared.email import EmailMessage
from shared.errors import ConflictError, NotFoundError
from shared.identity import (
    AuthTokenRecord,
    WorkspaceInvitationRole,
    WorkspaceInvitationStatus,
    WorkspaceRole,
)
from tests.service_fixtures import owned_workspace

from billing import DatabaseBillingAdmission


@dataclass(slots=True)
class _Outbox:
    sent: list[EmailMessage] = field(default_factory=list)

    def send(self, message: EmailMessage) -> None:
        self.sent.append(message)


def _service(services: ApiServices, outbox: _Outbox) -> WorkspaceInvitationService:
    return WorkspaceInvitationService(
        services.context,
        mailer=lambda: outbox,
        invitations_url="https://lazycloud.test/invitations",
    )


def _account(services: ApiServices, name: str, email: str) -> tuple[str, AuthTokenRecord]:
    user = services.users.create(display_name=name)
    return _signed_in(services, user.id, name, email)


def _owner(services: ApiServices, workspace_id: str, email: str) -> tuple[str, AuthTokenRecord]:
    """The fixture's owner, on a plan with room for the people these tests invite.

    The free plan is one seat and the owner holds it, so every invitation against
    it is refused before anything about invitations is exercised. That refusal is
    real and is proven where it belongs, in the billing owner's admission tests.
    """
    owner = next(m for m in services.users.members(workspace_id) if m.role is WorkspaceRole.Owner)
    with services.context.database.session() as session:
        BillingAccountRepository(session).upsert(
            user_id=owner.user_id,
            status=BillingAccountStatus.Active,
            provider_customer_id=f"cus_{owner.user_id}",
            provider_subscription_id=f"sub_{owner.user_id}",
            provider_credit_grant_id=f"credgr_{owner.user_id}",
            plan=BillingPlanId.Team,
        )
    return _signed_in(services, owner.user_id, "owner", email)


def _signed_in(
    services: ApiServices, user_id: str, name: str, email: str
) -> tuple[str, AuthTokenRecord]:
    with services.context.database.session() as session:
        UserRepository(session).set_profile(user_id, display_name=name, email=email, avatar_url="")
    _raw, token = AuthService(services.context).create_account_token(user_id, f"{name}-cli")
    return user_id, token


def test_only_the_addressed_account_can_answer_an_invitation(
    isolated_services: ApiServices,
) -> None:
    """The address on the invitation is the one place an email decides identity.

    Somebody else who learns the invitation id gets "not found", the same answer
    as for an id that never existed, and the offer stays open for the person it
    was made to. Answering it twice cannot seat them twice, and the address is
    matched folded, so the case it was typed in does not decide who may accept.
    """
    workspace = owned_workspace(isolated_services.control_plane_service, "team")
    _owner_id, owner = _owner(isolated_services, workspace.id, "owner@example.test")
    outbox = _Outbox()
    service = _service(isolated_services, outbox)

    listing = service.invite(
        workspace.id,
        email="Invited@Example.test",
        role=WorkspaceInvitationRole.Administrator,
        actor=owner,
        admission=DatabaseBillingAdmission(),
    )
    assert listing.invitation.email == "invited@example.test"
    assert [message.to for message in outbox.sent] == ["invited@example.test"]
    assert "https://lazycloud.test/invitations" in outbox.sent[0].text

    with pytest.raises(ConflictError):
        service.invite(
            workspace.id,
            email="invited@example.test",
            role=WorkspaceInvitationRole.Member,
            actor=owner,
            admission=DatabaseBillingAdmission(),
        )

    stranger_id, stranger = _account(isolated_services, "stranger", "stranger@example.test")
    assert service.pending_for_user(stranger_id) == []
    with pytest.raises(NotFoundError):
        service.accept(listing.invitation.id, actor=stranger, admission=DatabaseBillingAdmission())

    invited_id, invited = _account(isolated_services, "invited", "INVITED@example.test")
    assert [item.workspace.name for item in service.pending_for_user(invited_id)] == ["team"]
    accepted = service.accept(
        listing.invitation.id, actor=invited, admission=DatabaseBillingAdmission()
    )
    assert accepted.membership.role is WorkspaceRole.Administrator
    assert isolated_services.users.membership(workspace_id=workspace.id, user_id=invited_id)
    with pytest.raises(ConflictError):
        service.accept(listing.invitation.id, actor=invited, admission=DatabaseBillingAdmission())
    assert service.pending(workspace.id) == []

    # The address is now a member, so a fresh offer to it is refused outright.
    with pytest.raises(ConflictError):
        service.invite(
            workspace.id,
            email="invited@example.test",
            role=WorkspaceInvitationRole.Member,
            actor=owner,
            admission=DatabaseBillingAdmission(),
        )


def test_an_expired_or_revoked_invitation_cannot_be_accepted(
    isolated_services: ApiServices,
) -> None:
    workspace = owned_workspace(isolated_services.control_plane_service, "team")
    _owner_id, owner = _owner(isolated_services, workspace.id, "owner@example.test")
    service = _service(isolated_services, _Outbox())
    invited_id, invited = _account(isolated_services, "invited", "invited@example.test")

    revoked = service.invite(
        workspace.id,
        email="invited@example.test",
        role=WorkspaceInvitationRole.Member,
        actor=owner,
        admission=DatabaseBillingAdmission(),
    )
    assert (
        service.revoke(workspace.id, revoked.invitation.id, actor=owner).status
        is WorkspaceInvitationStatus.Revoked
    )
    with pytest.raises(ConflictError):
        service.accept(revoked.invitation.id, actor=invited, admission=DatabaseBillingAdmission())

    expiring = WorkspaceInvitationService(
        isolated_services.context,
        mailer=_Outbox,
        invitations_url="https://lazycloud.test/invitations",
        ttl=timedelta(seconds=-1),
    ).invite(
        workspace.id,
        email="invited@example.test",
        role=WorkspaceInvitationRole.Member,
        actor=owner,
        admission=DatabaseBillingAdmission(),
    )
    assert service.pending_for_user(invited_id) == []
    with pytest.raises(ConflictError):
        service.accept(expiring.invitation.id, actor=invited, admission=DatabaseBillingAdmission())
    # Resending is what brings an expired offer back.
    service.resend(workspace.id, expiring.invitation.id, actor=owner)
    assert [item.invitation.id for item in service.pending_for_user(invited_id)] == [
        expiring.invitation.id
    ]
    assert isolated_services.users.membership(workspace_id=workspace.id, user_id=invited_id) is None


def test_accepting_grants_the_role_the_offer_named(isolated_services: ApiServices) -> None:
    """An offer outstanding when somebody is added directly is still an admin's decision.

    Consuming it silently would leave the person with the lesser role an
    unrelated action gave them and no invitation left to resend.
    """
    workspace = owned_workspace(isolated_services.control_plane_service, "team")
    _owner_id, owner = _owner(isolated_services, workspace.id, "owner@example.test")
    service = _service(isolated_services, _Outbox())
    invited_id, invited = _account(isolated_services, "invited", "invited@example.test")

    listing = service.invite(
        workspace.id,
        email="invited@example.test",
        role=WorkspaceInvitationRole.Administrator,
        actor=owner,
        admission=DatabaseBillingAdmission(),
    )
    isolated_services.users.add_member(
        workspace_id=workspace.id,
        user_id=invited_id,
        role=WorkspaceRole.Member,
        admission=DatabaseBillingAdmission(),
    )

    accepted = service.accept(
        listing.invitation.id, actor=invited, admission=DatabaseBillingAdmission()
    )

    assert accepted.membership.role is WorkspaceRole.Administrator


def test_a_member_may_leave_but_the_owner_may_not(isolated_services: ApiServices) -> None:
    workspace = owned_workspace(isolated_services.control_plane_service, "team")
    owner_id, owner = _owner(isolated_services, workspace.id, "owner@example.test")
    member_id, member = _account(isolated_services, "member", "member@example.test")
    isolated_services.users.add_member(
        workspace_id=workspace.id,
        user_id=member_id,
        admission=DatabaseBillingAdmission(),
    )

    assert isolated_services.users.remove_member(
        workspace_id=workspace.id, user_id=member_id, actor=member, leaving=True
    )
    assert isolated_services.users.membership(workspace_id=workspace.id, user_id=member_id) is None
    with pytest.raises(ConflictError):
        isolated_services.users.remove_member(
            workspace_id=workspace.id, user_id=owner_id, actor=owner, leaving=True
        )
