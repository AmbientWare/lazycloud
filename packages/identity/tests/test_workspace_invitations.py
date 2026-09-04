from __future__ import annotations

from datetime import timedelta

import pytest
from api.server.services import ApiServices
from database.repositories.billing import BillingAccountRepository
from database.repositories.email_outbox import EmailOutboxRepository
from database.repositories.identity import UserRepository
from identity.auth import AuthService
from identity.invitations import WorkspaceInvitationService
from shared.billing_accounts import BillingAccountStatus
from shared.billing_plans import BillingPlanId
from shared.errors import ConflictError, NotFoundError
from shared.identity import AuthTokenRecord, WorkspaceInvitationRole, WorkspaceRole
from shared.timestamps import utc_now
from tests.service_fixtures import owned_workspace

from billing import DatabaseBillingAdmission

_INVITATIONS_URL = "https://lazycloud.test/invitations"


def _service(services: ApiServices, *, ttl: timedelta | None = None) -> WorkspaceInvitationService:
    return WorkspaceInvitationService(
        services.context,
        invitations_url=_INVITATIONS_URL,
        **({"ttl": ttl} if ttl is not None else {}),
    )


def _queued_links(services: ApiServices) -> list[str]:
    """The links the outbox is holding, read the way the drain reads them.

    Through the queue rather than a return value, because the link is only ever
    in the message: nothing hands it back to the caller who sent the invitation,
    which is what stops it being logged beside a request.
    """
    with services.context.database.session() as session:
        claimed = EmailOutboxRepository(session).claim(
            now=utc_now(),
            limit=50,
            claim_token="test-drain",
        )
    prefix = f"{_INVITATIONS_URL}/"
    return [
        line[len(prefix) :].strip()
        for item in claimed
        for line in item.message.text.splitlines()
        if line.startswith(prefix)
    ]


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


def test_the_link_is_what_joins_and_it_works_once(isolated_services: ApiServices) -> None:
    """Holding the link is the claim, and the account redeeming it is the member.

    Deliberately an account whose address is nothing like the one invited: an
    offer keyed on the address would refuse the person it was sent to as soon as
    they changed their email, which is the failure this design removes. The
    second redemption finds no row, because accepting deleted it.
    """
    workspace = owned_workspace(isolated_services.control_plane_service, "team")
    _owner_id, owner = _owner(isolated_services, workspace.id, "owner@example.test")
    service = _service(isolated_services)

    service.invite(
        workspace.id,
        email="Invited@Example.test",
        role=WorkspaceInvitationRole.Administrator,
        actor=owner,
        admission=DatabaseBillingAdmission(),
    )
    token = _queued_links(isolated_services)[0]

    joiner_id, joiner = _account(isolated_services, "joiner", "different@elsewhere.test")
    preview = service.preview(token)
    assert preview.workspace.name == "team"
    assert preview.invitation.email == "invited@example.test"
    assert not preview.expired

    accepted = service.accept(token, actor=joiner, admission=DatabaseBillingAdmission())

    assert accepted.membership.role is WorkspaceRole.Administrator
    assert accepted.user.id == joiner_id
    assert service.open_offers(workspace.id) == []
    with pytest.raises(NotFoundError):
        service.accept(token, actor=joiner, admission=DatabaseBillingAdmission())


def test_a_resent_offer_replaces_the_link_the_first_message_carried(
    isolated_services: ApiServices,
) -> None:
    """A resend exists because the first message went astray.

    Leaving its link live would leave whatever it went astray into holding a way
    in, so the old one stops opening anything.
    """
    workspace = owned_workspace(isolated_services.control_plane_service, "team")
    _owner_id, owner = _owner(isolated_services, workspace.id, "owner@example.test")
    service = _service(isolated_services)
    listing = service.invite(
        workspace.id,
        email="invited@example.test",
        role=WorkspaceInvitationRole.Member,
        actor=owner,
        admission=DatabaseBillingAdmission(),
    )
    first = _queued_links(isolated_services)[0]

    service.resend(workspace.id, listing.invitation.id, actor=owner)
    second = _queued_links(isolated_services)[0]

    assert first != second
    _joiner_id, joiner = _account(isolated_services, "joiner", "joiner@example.test")
    with pytest.raises(NotFoundError):
        service.accept(first, actor=joiner, admission=DatabaseBillingAdmission())
    assert service.accept(second, actor=joiner, admission=DatabaseBillingAdmission())


def test_revoking_takes_the_offer_off_the_table(isolated_services: ApiServices) -> None:
    workspace = owned_workspace(isolated_services.control_plane_service, "team")
    _owner_id, owner = _owner(isolated_services, workspace.id, "owner@example.test")
    service = _service(isolated_services)
    listing = service.invite(
        workspace.id,
        email="invited@example.test",
        role=WorkspaceInvitationRole.Member,
        actor=owner,
        admission=DatabaseBillingAdmission(),
    )
    token = _queued_links(isolated_services)[0]

    service.revoke(workspace.id, listing.invitation.id, actor=owner)

    assert service.open_offers(workspace.id) == []
    _joiner_id, joiner = _account(isolated_services, "joiner", "joiner@example.test")
    with pytest.raises(NotFoundError):
        service.accept(token, actor=joiner, admission=DatabaseBillingAdmission())


def test_expiry_is_the_servers_answer_and_an_expired_link_joins_nobody(
    isolated_services: ApiServices,
) -> None:
    workspace = owned_workspace(isolated_services.control_plane_service, "team")
    _owner_id, owner = _owner(isolated_services, workspace.id, "owner@example.test")
    expiring = _service(isolated_services, ttl=timedelta(seconds=-1))
    expiring.invite(
        workspace.id,
        email="invited@example.test",
        role=WorkspaceInvitationRole.Member,
        actor=owner,
        admission=DatabaseBillingAdmission(),
    )
    token = _queued_links(isolated_services)[0]
    service = _service(isolated_services)

    assert [item.expired for item in service.open_offers(workspace.id)] == [True]
    assert service.preview(token).expired
    _joiner_id, joiner = _account(isolated_services, "joiner", "joiner@example.test")
    with pytest.raises(ConflictError):
        service.accept(token, actor=joiner, admission=DatabaseBillingAdmission())


def test_accepting_grants_the_role_the_offer_named(isolated_services: ApiServices) -> None:
    """An offer outstanding when somebody is added directly is still an admin's decision."""
    workspace = owned_workspace(isolated_services.control_plane_service, "team")
    _owner_id, owner = _owner(isolated_services, workspace.id, "owner@example.test")
    service = _service(isolated_services)
    service.invite(
        workspace.id,
        email="invited@example.test",
        role=WorkspaceInvitationRole.Administrator,
        actor=owner,
        admission=DatabaseBillingAdmission(),
    )
    token = _queued_links(isolated_services)[0]
    joiner_id, joiner = _account(isolated_services, "joiner", "joiner@example.test")
    isolated_services.users.add_member(
        workspace_id=workspace.id,
        user_id=joiner_id,
        role=WorkspaceRole.Member,
        admission=DatabaseBillingAdmission(),
    )

    accepted = service.accept(token, actor=joiner, admission=DatabaseBillingAdmission())

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
